import requests
import json
import zipfile
import os
import unicodedata
from datetime import datetime, timedelta

# --- KONFIGURACE API ---
API_URL = "https://feda.hafas.cloud/gate/"
AID = "jf784LdHu4KNBfUc"

ANDORRA_HOLIDAYS = ["01-01", "03-14", "05-01", "09-08", "12-25", "12-26"]

# --- POMOCNÉ TŘÍDY A FUNKCE ---
class TimeTracker:
    """Hlídá přejezdy přes půlnoc pro zachování plynulého GTFS času."""
    def __init__(self, shift_24h=False):
        self.prev_base_secs = -1
        self.rollover_days = 0
        self.shift_24h = shift_24h

    def parse(self, hafas_time, day_offset=0):
        if not hafas_time:
            return ""

        # HAFAS může vrátit 8 znaků (DDHHMMSS) nebo 6 znaků (HHMMSS)
        embedded_days = 0
        if len(hafas_time) == 8:
            embedded_days = int(hafas_time[:2])
            h = int(hafas_time[2:4])
            m = int(hafas_time[4:6])
            s = int(hafas_time[6:8])
        elif len(hafas_time) == 6:
            h = int(hafas_time[:2])
            m = int(hafas_time[2:4])
            s = int(hafas_time[4:6])
        else:
            return ""

        # Pokud HAFAS pošle hodiny nad 24 (např. 25:00:00), den už je započítán
        if h >= 24:
            added_days = 0
        else:
            # Zabráníme dvojímu sečtení offsetů
            added_days = max(day_offset, embedded_days)

        base_secs = (h + added_days * 24) * 3600 + m * 60 + s

        # Pokud je to noční spoj bez HAFAS offsetu (čas např. 00:23:00, tedy < 6h ráno),
        # posuneme ho do předchozího provozního dne (na 24:23:00)
        if self.shift_24h and base_secs < 6 * 3600:
            base_secs += 24 * 3600

        # Průběžný přejezd přes půlnoc v rámci jedné jízdy
        if self.prev_base_secs != -1 and base_secs < self.prev_base_secs - (12 * 3600):
            self.rollover_days += 1

        self.prev_base_secs = base_secs
        total_secs = base_secs + (self.rollover_days * 24 * 3600)

        final_h = total_secs // 3600
        final_m = (total_secs % 3600) // 60
        final_s = total_secs % 60

        return f"{final_h:02d}:{final_m:02d}:{final_s:02d}"

def clean_ascii(text):
    """Odstraní diakritiku z ID, aby GTFS validátor nehlásil non-ascii chars."""
    normalized = unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('utf-8')
    return normalized.replace(" ", "_").upper()

def decode_polyline(polyline_str):
    index, lat, lng, length = 0, 0, 0, len(polyline_str)
    coordinates = []
    while index < length:
        shift, result = 0, 0
        while True:
            byte = ord(polyline_str[index]) - 63
            index += 1; result |= (byte & 0x1f) << shift; shift += 5
            if byte < 0x20: break
        lat += ~(result >> 1) if (result & 1) else (result >> 1)

        shift, result = 0, 0
        while True:
            byte = ord(polyline_str[index]) - 63
            index += 1; result |= (byte & 0x1f) << shift; shift += 5
            if byte < 0x20: break
        lng += ~(result >> 1) if (result & 1) else (result >> 1)
        coordinates.append((lat / 1e5, lng / 1e5))
    return coordinates

def call_hafas(method, req_data):
    payload = {
        "ver": "1.63", "lang": "cat",
        "auth": {"type": "AID", "aid": AID},
        "client": {"id": "HAFAS", "type": "WEB", "name": "webapp", "l": "vs_webapp", "v": 10005},
        "formatted": False,
        "svcReqL": [{"meth": method, "req": req_data, "id": "1|1|"}]
    }
    resp = requests.post(API_URL, json=payload, headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    return resp.json()

def get_next_day_of_week(start_date, target_weekday):
    d = start_date
    while d.weekday() != target_weekday:
        d += timedelta(days=1)
    return d

# --- HLAVNÍ SKRIPT ---
if __name__ == "__main__":
    start_time = datetime.now()
    today = datetime.now()
    end_date = today + timedelta(days=180)

    today_str = today.strftime("%Y%m%d")
    end_date_str = end_date.strftime("%Y%m%d")

    master_stops = {}

    print("1. Hledám uzlové zastávky pro plošný sken odjezdů...")
    stops_data = call_hafas("LocGeoPos", {
        "ring": {"cCrd": {"x": 1521173, "y": 42504451}, "maxDist": 30000},
        "getPOIs": False, "getStops": True
    })

    seed_stop_ids = []
    for loc in stops_data.get('svcResL', [{}])[0].get('res', {}).get('locL', []):
        ext_id = loc.get('extId')
        if ext_id:
            seed_stop_ids.append(ext_id)
            if 'crd' in loc:
                master_stops[ext_id] = {
                    "name": loc.get('name', 'Neznámá zastávka'),
                    "lat": loc['crd']['y'] / 1000000,
                    "lon": loc['crd']['x'] / 1000000
                }

    print(f"-> Zahajuji sken přes {len(seed_stop_ids)} počátečních bodů.")

    # Skenujeme středu (typický pracovní den) a sobotu (typický víkend vč. páteční noci)
    next_wednesday = get_next_day_of_week(today, 2).strftime("%Y%m%d")
    next_saturday = get_next_day_of_week(today, 5).strftime("%Y%m%d")
    jid_service_map = {}

    print(f"\n2. Skenuji spoje pro dny {next_wednesday} (Středa) a {next_saturday} (Sobota)...")
    for i, stop_id in enumerate(seed_stop_ids, 1):
        if i % 25 == 0:
            print(f"-> Skenuji uzel {i}/{len(seed_stop_ids)}")
        try:
            stb_wd = call_hafas("StationBoard", {
                "type": "DEP", "stbLoc": {"type": "S", "extId": stop_id},
                "maxJny": 1000, "date": next_wednesday, "time": "000000", "dur": 1440
            })
            for jny in stb_wd.get('svcResL', [{}])[0].get('res', {}).get('jnyL', []):
                jid_service_map.setdefault(jny['jid'], set()).add("WD")

            stb_we = call_hafas("StationBoard", {
                "type": "DEP", "stbLoc": {"type": "S", "extId": stop_id},
                "maxJny": 1000, "date": next_saturday, "time": "000000", "dur": 1440
            })
            for jny in stb_we.get('svcResL', [{}])[0].get('res', {}).get('jnyL', []):
                jid_service_map.setdefault(jny['jid'], set()).add("WE")
        except Exception: pass

    total_jids = len(jid_service_map)
    print(f"-> Objeveno {total_jids} unikátních spojů.")

    print("\n3. Stahuji jízdní řády, trasy a sbírám všechny zastávky...")

    routes_dict = {}
    shapes_dict = {}
    trips_list = []
    stop_times_list = []

    for processed, (jid, service_flags) in enumerate(jid_service_map.items(), 1):
        if processed % 50 == 0 or processed == total_jids:
            print(f"-> Zpracováno {processed}/{total_jids} spojů...")

        try:
            jd_data = call_hafas("JourneyDetails", {
                "jid": jid, "getPolyline": True, "getPasslist": True
            })
            res = jd_data['svcResL'][0]['res']
            journey = res['journey']
            common_lines = res.get('common', {}).get('prodL', [])
            common_locs = res.get('common', {}).get('locL', [])
            common_polys = res.get('common', {}).get('polyL', [])

            for loc in common_locs:
                extId = loc.get('extId')
                if extId and 'crd' in loc and extId not in master_stops:
                    master_stops[extId] = {
                        "name": loc.get('name', 'Neznámá zastávka'),
                        "lat": loc['crd']['y'] / 1000000,
                        "lon": loc['crd']['x'] / 1000000
                    }

            line_idx = journey.get('prodX')
            line_info = common_lines[line_idx] if line_idx is not None else {}
            raw_short_name = line_info.get('nameS', 'Bus')
            long_name = line_info.get('name', f'Bus {raw_short_name}')

            route_id = f"ROUTE_{clean_ascii(raw_short_name)}"

            if route_id not in routes_dict:
                routes_dict[route_id] = {
                    "short_name": raw_short_name,
                    "long_name": long_name
                }

            shape_id = ""
            poly_indices = journey.get('polyG', {}).get('polyXL', [])
            if poly_indices and poly_indices[0] < len(common_polys):
                encoded_poly = common_polys[poly_indices[0]].get('crdEncYX', '')
                if encoded_poly:
                    if encoded_poly not in shapes_dict:
                        shapes_dict[encoded_poly] = f"shape_{len(shapes_dict) + 1}"
                    shape_id = shapes_dict[encoded_poly]

            headsign = journey.get('dirTxt', 'Směr neznámý').title()

            # Zjištění, zda jde o noční bus podle názvu (BN1, Bus Nocturn, NIT...)
            is_night_bus = (
                raw_short_name.upper().startswith("BN") or
                "NOCTURN" in raw_short_name.upper() or
                "NOCTURN" in long_name.upper() or
                "NIT" in raw_short_name.upper() or
                "NIT" in long_name.upper()
            )

            # Přiřazení GTFS Service ID podle provozního vzorce
            if "WD" in service_flags and "WE" in service_flags:
                assigned_service_id = "SERVICE_NIGHT_DAILY" if is_night_bus else "SERVICE_DAILY"
            elif "WD" in service_flags:
                assigned_service_id = "SERVICE_NIGHT_WD" if is_night_bus else "SERVICE_WEEKDAY"
            else:
                assigned_service_id = "SERVICE_NIGHT_WE" if is_night_bus else "SERVICE_WEEKEND"

            trips_list.append((route_id, assigned_service_id, jid, headsign, shape_id))

            # Kontrola času pro logický posun (shift) popůlnočních spojů do předchozího dne
            shift_24h = False
            first_time_str = None
            for stop in journey.get('stopL', []):
                first_time_str = stop.get('dTimeS', stop.get('aTimeS'))
                if first_time_str:
                    break

            if is_night_bus and first_time_str:
                first_h = int(first_time_str[:2])
                if first_h < 6:  # Spoj začíná před 6. ranní, patří ale k nočnímu provozu předchozího dne
                    shift_24h = True

            timer = TimeTracker(shift_24h=shift_24h)
            stop_seq = 1
            for stop in journey.get('stopL', []):
                loc_idx = stop.get('locX')
                if loc_idx is not None and loc_idx < len(common_locs):
                    station_id = common_locs[loc_idx].get('extId', '')

                    a_str = stop.get('aTimeS', stop.get('dTimeS'))
                    d_str = stop.get('dTimeS', stop.get('aTimeS'))
                    a_off = stop.get('aDayOff', stop.get('dDayOff', 0))
                    d_off = stop.get('dDayOff', stop.get('aDayOff', 0))

                    arr_time = timer.parse(a_str, a_off)
                    dep_time = timer.parse(d_str, d_off)

                    if station_id and arr_time and dep_time:
                        stop_times_list.append((jid, arr_time, dep_time, station_id, stop_seq))
                        stop_seq += 1

        except Exception:
            continue

    print("\n4. Vytvářím 100% validní CSV soubory...")

    with open("stops.txt", "w", encoding="utf-8") as f:
        f.write("stop_id,stop_name,stop_lat,stop_lon\n")
        for sid, sdata in master_stops.items():
            f.write(f'"{sid}","{sdata["name"]}",{sdata["lat"]},{sdata["lon"]}\n')

    with open("routes.txt", "w", encoding="utf-8") as f:
        f.write("route_id,agency_id,route_short_name,route_long_name,route_type\n")
        for rid, rdata in routes_dict.items():
            f.write(f'"{rid}","FEDA","{rdata["short_name"]}","{rdata["long_name"]}",3\n')

    with open("trips.txt", "w", encoding="utf-8") as f:
        f.write("route_id,service_id,trip_id,trip_headsign,shape_id\n")
        for t in trips_list:
            f.write(f'"{t[0]}","{t[1]}","{t[2]}","{t[3]}","{t[4]}"\n')

    with open("stop_times.txt", "w", encoding="utf-8") as f:
        f.write("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n")
        for st in stop_times_list:
            f.write(f'"{st[0]}","{st[1]}","{st[2]}","{st[3]}",{st[4]}\n')

    with open("shapes.txt", "w", encoding="utf-8") as f:
        f.write("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n")
        for encoded_poly, shp_id in shapes_dict.items():
            coords = decode_polyline(encoded_poly)
            for seq, (lat, lon) in enumerate(coords, 1):
                f.write(f'"{shp_id}",{lat},{lon},{seq}\n')

    with open("agency.txt", "w", encoding="utf-8") as f:
        f.write("agency_id,agency_name,agency_url,agency_timezone\n")
        f.write('"FEDA","Mou-te Andorra","https://www.fedasolucions.ad","Europe/Andorra"\n')

    with open("feed_info.txt", "w", encoding="utf-8") as f:
        f.write("feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date,feed_version,feed_contact_email,feed_contact_url\n")
        f.write(f'"Mou-te Andorra","https://www.fedasolucions.ad","ca",{today_str},{end_date_str},"1.0","info@bus.ad","https://bus.ad/contacte/"\n')

    with open("calendar.txt", "w", encoding="utf-8") as f:
        f.write("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n")
        f.write(f'"SERVICE_WEEKDAY",1,1,1,1,1,0,0,{today_str},{end_date_str}\n')
        f.write(f'"SERVICE_WEEKEND",0,0,0,0,0,1,1,{today_str},{end_date_str}\n')
        f.write(f'"SERVICE_DAILY",1,1,1,1,1,1,1,{today_str},{end_date_str}\n')

        # Noční provozní šablony podle oficiálních jízdních řádů:
        # NIGHT_WD = provoz: Ne->Po, Po->Út, Út->St, St->Čt, Čt->Pá (takže pondělí-čtvrtek a neděle)
        f.write(f'"SERVICE_NIGHT_WD",1,1,1,1,0,0,1,{today_str},{end_date_str}\n')

        # NIGHT_WE = provoz: Pá->So, So->Ne (takže pátek a sobota)
        f.write(f'"SERVICE_NIGHT_WE",0,0,0,0,1,1,0,{today_str},{end_date_str}\n')
        f.write(f'"SERVICE_NIGHT_DAILY",1,1,1,1,1,1,1,{today_str},{end_date_str}\n')

    with open("calendar_dates.txt", "w", encoding="utf-8") as f:
        f.write("service_id,date,exception_type\n")
        current_date = today
        while current_date <= end_date:
            mm_dd = current_date.strftime("%m-%d")
            date_str = current_date.strftime("%Y%m%d")

            # Výjimky pro DENNÍ linky přímo v den svátku
            if mm_dd in ANDORRA_HOLIDAYS and current_date.weekday() < 5:
                f.write(f'"SERVICE_WEEKDAY",{date_str},2\n')
                f.write(f'"SERVICE_WEEKEND",{date_str},1\n')

            # Výjimky pro NOČNÍ linky musí reagovat v PŘEDVEČER svátku
            next_day = current_date + timedelta(days=1)
            next_mm_dd = next_day.strftime("%m-%d")
            if next_mm_dd in ANDORRA_HOLIDAYS and current_date.weekday() not in [4, 5]:
                # Pokud zítra je svátek a dnes není Pá/So (které už mají noční víkend),
                # nahradíme běžný noční provoz víkendovým.
                f.write(f'"SERVICE_NIGHT_WD",{date_str},2\n')
                f.write(f'"SERVICE_NIGHT_WE",{date_str},1\n')

            current_date += timedelta(days=1)

    print("5. Balím vše do archivu andorra_gtfs.zip...")
    files_to_zip = ["agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "shapes.txt", "calendar.txt", "calendar_dates.txt", "feed_info.txt"]

    with zipfile.ZipFile("andorra_gtfs.zip", "w", zipfile.ZIP_DEFLATED) as zipf:
        for file in files_to_zip:
            if os.path.exists(file):
                zipf.write(file)
                os.remove(file)

    exec_time = datetime.now() - start_time
    print(f"\n--- HOTOVO! ---")
    print(f"Zpracováno za {exec_time.seconds} vteřin. Nový ZIP archiv je připraven.")
