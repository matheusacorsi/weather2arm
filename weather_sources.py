from __future__ import annotations

import csv
import io
import math
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

API_HOURLY = "https://power.larc.nasa.gov/api/temporal/hourly/point"
API_DAILY = "https://power.larc.nasa.gov/api/temporal/daily/point"

HOURLY_VARIABLES = {"T2M", "RH2M", "WS2M", "WD2M"}
DAILY_VARIABLES = {"PRECTOTCORR"}
MISSING_MARKERS = {"", "NA", "NAN", "NULL", "-9999", "-999", "-99", "-999.0", "-9999.0"}


@dataclass
class WeatherBuildResult:
    daily_storage: Dict[str, Dict[str, object]]
    hourly_records: List[Dict[str, object]]
    metadata: Dict[str, object]


def normalize_text(value: str) -> str:
    txt = unicodedata.normalize("NFKD", str(value or ""))
    txt = txt.encode("ascii", "ignore").decode("ascii")
    txt = re.sub(r"[^A-Za-z0-9]+", " ", txt)
    return re.sub(r"\s+", " ", txt).strip().upper()


def parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    txt = str(value).strip()
    if normalize_text(txt) in MISSING_MARKERS:
        return None
    txt = txt.replace(".", "").replace(",", ".") if txt.count(",") == 1 and txt.count(".") > 1 else txt.replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def year_chunks(start_d: date, end_d: date) -> List[Tuple[date, date]]:
    chunks: List[Tuple[date, date]] = []
    cur_start = start_d
    while cur_start <= end_d:
        year_end = date(cur_start.year, 12, 31)
        cur_end = min(year_end, end_d)
        chunks.append((cur_start, cur_end))
        cur_start = cur_end + timedelta(days=1)
    return chunks


def iter_clean_rows(csv_text: str):
    lines = csv_text.splitlines()
    clean_lines = [line for line in lines if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("-END")]
    if not clean_lines:
        return None, []
    header_idx = -1
    header_row = []
    for i, line in enumerate(clean_lines):
        row = next(csv.reader(io.StringIO(line)))
        row_upper = [c.upper().strip() for c in row]
        if any(x in row_upper for x in ["YEAR", "YYYY", "YR"]):
            header_idx = i
            header_row = row_upper
            break
    if header_idx == -1:
        return None, []
    data_rows = []
    for line in clean_lines[header_idx + 1 :]:
        row = next(csv.reader(io.StringIO(line)))
        if row:
            data_rows.append(row)
    return header_row, data_rows


def http_get_with_retries(url: str, params: Dict[str, object], timeout: int = 90, max_retries: int = 4, backoff: float = 2.0, verify: bool = False):
    headers = {"User-Agent": "NASA-POWER-INMET-Downloader/2.0"}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=headers, verify=verify)
            if resp.status_code == 200:
                return True, resp
            msg = f"HTTP {resp.status_code}"
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(backoff ** attempt)
                continue
            return False, msg
        except Exception as exc:  # pragma: no cover - network failure branch
            msg = f"Error: {exc}"
        if attempt < max_retries:
            time.sleep(backoff ** attempt)
            continue
        return False, msg


class NasaPowerProvider:
    def __init__(self, ssl_verify: bool = False):
        self.ssl_verify = ssl_verify

    def fetch_hourly(self, lat: float, lon: float, start_date: date, end_date: date, community: str, params: List[str], tstd: str) -> pd.DataFrame:
        records: List[Dict[str, object]] = []
        if not params:
            return pd.DataFrame(records)
        for cstart, cend in year_chunks(start_date, end_date):
            q = {
                "parameters": ",".join(params),
                "community": community,
                "longitude": lon,
                "latitude": lat,
                "start": cstart.strftime("%Y%m%d"),
                "end": cend.strftime("%Y%m%d"),
                "format": "CSV",
                "time-standard": tstd,
            }
            ok, res = http_get_with_retries(API_HOURLY, q, verify=self.ssl_verify)
            if not ok:
                continue

            header, rows = iter_clean_rows(res.text)
            if not header:
                continue

            map_idx = {h: i for i, h in enumerate(header)}
            ix_y = next((map_idx.get(k) for k in ["YEAR", "YYYY", "YR"] if k in map_idx), None)
            ix_m = next((map_idx.get(k) for k in ["MO", "MM", "MONTH"] if k in map_idx), None)
            ix_d = next((map_idx.get(k) for k in ["DY", "DD", "DAY"] if k in map_idx), None)
            ix_h = next((map_idx.get(k) for k in ["HR", "HH", "HOUR"] if k in map_idx), None)
            if ix_y is None or ix_m is None or ix_d is None:
                continue

            for row in rows:
                try:
                    y = int(float(row[ix_y]))
                    m = int(float(row[ix_m]))
                    d = int(float(row[ix_d]))
                    hour = int(float(row[ix_h])) if ix_h is not None else 0
                    dt = datetime(y, m, d, hour)
                except Exception:
                    continue

                rec: Dict[str, object] = {"dt": dt, "date_key": f"{y:04d}{m:02d}{d:02d}", "hr": str(hour)}
                for p in params:
                    col_idx = map_idx.get(p)
                    if col_idx is None:
                        rec[p] = None
                    else:
                        val = parse_float(row[col_idx])
                        rec[p] = None if val is None or val == -999.0 else val
                records.append(rec)

        if not records:
            return pd.DataFrame(columns=["dt", "date_key", "hr"] + params)
        df = pd.DataFrame(records).drop_duplicates(subset=["dt"], keep="first").sort_values("dt")
        return df

    def fetch_daily(self, lat: float, lon: float, start_date: date, end_date: date, community: str, params: List[str], tstd: str) -> pd.DataFrame:
        records: List[Dict[str, object]] = []
        if not params:
            return pd.DataFrame(records)
        for cstart, cend in year_chunks(start_date, end_date):
            q = {
                "parameters": ",".join(params),
                "community": community,
                "longitude": lon,
                "latitude": lat,
                "start": cstart.strftime("%Y%m%d"),
                "end": cend.strftime("%Y%m%d"),
                "format": "CSV",
                "time-standard": tstd,
            }
            ok, res = http_get_with_retries(API_DAILY, q, verify=self.ssl_verify)
            if not ok:
                continue

            header, rows = iter_clean_rows(res.text)
            if not header:
                continue

            map_idx = {h: i for i, h in enumerate(header)}
            ix_y = next((map_idx.get(k) for k in ["YEAR", "YYYY", "YR"] if k in map_idx), None)
            ix_m = next((map_idx.get(k) for k in ["MO", "MM", "MONTH"] if k in map_idx), None)
            ix_d = next((map_idx.get(k) for k in ["DY", "DD", "DAY"] if k in map_idx), None)
            ix_doy = next((map_idx.get(k) for k in ["DOY"] if k in map_idx), None)
            if ix_y is None:
                continue

            for row in rows:
                dt_obj: Optional[date] = None
                try:
                    if ix_m is not None and ix_d is not None:
                        dt_obj = date(int(float(row[ix_y])), int(float(row[ix_m])), int(float(row[ix_d])))
                    elif ix_doy is not None:
                        dt_obj = date(int(float(row[ix_y])), 1, 1) + timedelta(days=int(float(row[ix_doy])) - 1)
                except Exception:
                    continue
                if dt_obj is None:
                    continue

                rec: Dict[str, object] = {"date_key": dt_obj.strftime("%Y%m%d")}
                for p in params:
                    col_idx = map_idx.get(p)
                    if col_idx is None:
                        for h_name, h_idx in map_idx.items():
                            if h_name.startswith(p):
                                col_idx = h_idx
                                break
                    val = parse_float(row[col_idx]) if col_idx is not None else None
                    rec[p] = None if val is None or val == -999.0 else val
                records.append(rec)

        if not records:
            return pd.DataFrame(columns=["date_key"] + params)
        return pd.DataFrame(records).drop_duplicates(subset=["date_key"], keep="first").sort_values("date_key")


class RawInmetCsvSource:
    """Parses raw government INMET CSV/ZIP exports.

    Used only by scripts/convert_inmet_to_parquet.py to build the compact
    Parquet files that InmetProvider reads at runtime. Kept separate from
    InmetProvider so the app itself never needs to parse the verbose raw
    CSV format (19 columns, of which only 5 are ever used downstream).
    """

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self._meta_cache: Dict[str, Dict[str, object]] = {}

    def list_inmet_sources(self) -> List[Tuple[Path, Optional[str]]]:
        """
        Discover INMET data sources.
        Source tuple format: (file_path, zip_member).
        - For plain CSV files, zip_member is None.
        - For ZIP-contained CSV files, zip_member is the inner path.
        """
        if not self.data_dir.exists():
            return []

        sources: List[Tuple[Path, Optional[str]]] = []
        for p in sorted(self.data_dir.rglob("*")):
            if not p.is_file():
                continue
            ext = p.suffix.upper()
            if ext == ".CSV":
                sources.append((p, None))
            elif ext == ".ZIP":
                try:
                    with zipfile.ZipFile(p, "r") as zf:
                        for member in sorted(zf.namelist()):
                            if member.endswith("/"):
                                continue
                            if member.upper().endswith(".CSV"):
                                sources.append((p, member))
                except Exception:
                    # Skip malformed archives so one bad file does not break ingestion.
                    continue
        return sources

    def _read_text(self, file_path: Path) -> str:
        content = file_path.read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("latin-1", errors="replace")

    @staticmethod
    def _decode_bytes(content: bytes) -> str:
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("latin-1", errors="replace")

    def _read_source_head(self, source: Tuple[Path, Optional[str]], max_bytes: int = 131072) -> str:
        file_path, member = source
        if member is None:
            return self._decode_bytes(file_path.read_bytes()[:max_bytes])
        with zipfile.ZipFile(file_path, "r") as zf:
            with zf.open(member, "r") as fp:
                content = fp.read(max_bytes)
        return self._decode_bytes(content)

    def _read_source_text(self, source: Tuple[Path, Optional[str]]) -> str:
        file_path, member = source
        if member is None:
            return self._read_text(file_path)

        with zipfile.ZipFile(file_path, "r") as zf:
            content = zf.read(member)
        return self._decode_bytes(content)

    @staticmethod
    def _source_id(source: Tuple[Path, Optional[str]]) -> str:
        file_path, member = source
        if member is None:
            return str(file_path)
        return f"{file_path}!{member}"

    @staticmethod
    def _parse_source_id(source_id: str) -> Tuple[Path, Optional[str]]:
        if "!" not in source_id:
            return Path(source_id), None
        root, member = source_id.split("!", 1)
        return Path(root), member

    @staticmethod
    def _extract_date_range_from_name(source_id: str) -> Tuple[Optional[date], Optional[date]]:
        name = source_id.split("!", 1)[-1] if "!" in source_id else Path(source_id).name
        match = re.search(r"(\d{2}-\d{2}-\d{4})_A_(\d{2}-\d{2}-\d{4})", name)
        if not match:
            return None, None
        try:
            start_d = datetime.strptime(match.group(1), "%d-%m-%Y").date()
            end_d = datetime.strptime(match.group(2), "%d-%m-%Y").date()
            return start_d, end_d
        except ValueError:
            return None, None

    def parse_source_metadata(self, source: Tuple[Path, Optional[str]]) -> Dict[str, object]:
        source_id = self._source_id(source)
        if source_id in self._meta_cache:
            return self._meta_cache[source_id]

        text = self._read_source_head(source)
        lines = [line.rstrip("\n\r") for line in text.splitlines() if line.strip()]

        meta_raw: Dict[str, object] = {"file": source_id}
        for line in lines:
            if line.upper().startswith("DATA;"):
                break
            if ";" in line and ":" in line:
                k, v = line.split(";", 1)
                key = normalize_text(k.replace(":", ""))
                meta_raw[key] = v.strip()

        range_start, range_end = self._extract_date_range_from_name(source_id)
        meta = {
            "file": source_id,
            "station_name": str(meta_raw.get("ESTACAO", "")).strip(),
            "station_code": str(meta_raw.get("CODIGO WMO", "")).strip() or str(meta_raw.get("CODIGO", "")).strip(),
            "region": str(meta_raw.get("REGIAO", "")).strip(),
            "uf": str(meta_raw.get("UF", "")).strip(),
            "latitude": parse_float(meta_raw.get("LATITUDE")),
            "longitude": parse_float(meta_raw.get("LONGITUDE")),
            "altitude": parse_float(meta_raw.get("ALTITUDE")),
            "file_start_date": range_start,
            "file_end_date": range_end,
        }
        self._meta_cache[source_id] = meta
        return meta

    def parse_source(self, source: Tuple[Path, Optional[str]]) -> Tuple[Dict[str, object], pd.DataFrame]:
        source_id = self._source_id(source)

        text = self._read_source_text(source)
        lines = [line.rstrip("\n\r") for line in text.splitlines() if line.strip()]

        meta: Dict[str, object] = {"file": source_id}
        header_idx = None
        for i, line in enumerate(lines):
            if line.upper().startswith("DATA;"):
                header_idx = i
                break
            if ";" in line and ":" in line:
                k, v = line.split(";", 1)
                key = normalize_text(k.replace(":", ""))
                val = v.strip()
                meta[key] = val

        if header_idx is None:
            raise ValueError(f"INMET source missing table header row: {source_id}")

        table_csv = "\n".join(lines[header_idx:])
        raw_df = pd.read_csv(io.StringIO(table_csv), sep=";", dtype=str, engine="python")
        raw_df = raw_df.loc[:, [c for c in raw_df.columns if normalize_text(c) not in {"", "UNNAMED 0"}]]

        cols_norm = {c: normalize_text(c) for c in raw_df.columns}

        def find_col(*patterns: str) -> Optional[str]:
            norms = [normalize_text(p) for p in patterns]
            for col, col_norm in cols_norm.items():
                if all(tok in col_norm for tok in norms):
                    return col
            return None

        col_date = find_col("DATA")
        col_hour = find_col("HORA", "UTC")
        if not col_date or not col_hour:
            raise ValueError(f"INMET source missing date/hour columns: {source_id}")

        mappings = {
            "PRECTOTCORR": find_col("PRECIPITACAO", "TOTAL", "HORARIO"),
            "T2M": find_col("TEMPERATURA", "AR", "BULBO", "SECO", "HORARIA"),
            "RH2M": find_col("UMIDADE", "RELATIVA", "AR", "HORARIA"),
            "WD2M": find_col("VENTO", "DIRECAO", "HORARIA"),
            "WS2M": find_col("VENTO", "VELOCIDADE", "HORARIA"),
        }

        records: List[Dict[str, object]] = []
        for _, row in raw_df.iterrows():
            date_str = str(row.get(col_date, "")).strip()
            hour_raw = str(row.get(col_hour, "")).strip()
            if not date_str:
                continue
            hour_match = re.search(r"(\d{1,4})", hour_raw)
            if not hour_match:
                continue
            hour_num = int(hour_match.group(1).zfill(4)[:2])
            try:
                dt_utc = datetime.strptime(date_str, "%Y/%m/%d").replace(hour=hour_num, tzinfo=timezone.utc)
            except ValueError:
                continue

            rec: Dict[str, object] = {"dt_utc": dt_utc}
            for key, col in mappings.items():
                rec[key] = parse_float(row.get(col)) if col else None
            records.append(rec)

        df = pd.DataFrame(records, columns=["dt_utc", "PRECTOTCORR", "T2M", "RH2M", "WD2M", "WS2M"])
        if not df.empty:
            df = df.drop_duplicates(subset=["dt_utc"], keep="first").sort_values("dt_utc")
            for col in ("PRECTOTCORR", "T2M", "RH2M", "WD2M", "WS2M"):
                df[col] = df[col].astype("float32")

        lat = parse_float(meta.get("LATITUDE"))
        lon = parse_float(meta.get("LONGITUDE"))
        alt = parse_float(meta.get("ALTITUDE"))
        station_name = str(meta.get("ESTACAO", "")).strip()
        station_code = str(meta.get("CODIGO WMO", "")).strip() or str(meta.get("CODIGO", "")).strip()

        meta_out = {
            "file": source_id,
            "station_name": station_name,
            "station_code": station_code,
            "region": str(meta.get("REGIAO", "")).strip(),
            "uf": str(meta.get("UF", "")).strip(),
            "latitude": lat,
            "longitude": lon,
            "altitude": alt,
            "columns": mappings,
        }
        return meta_out, df


class InmetProvider:
    """Reads the compact Parquet files produced by scripts/convert_inmet_to_parquet.py.

    Expects `<data_dir>/stations.parquet` (station metadata) and one
    `<data_dir>/<year>.parquet` file per year of hourly data (dt_utc,
    station_code, PRECTOTCORR, T2M, RH2M, WD2M, WS2M).
    """

    DATA_COLUMNS = ["dt_utc", "station_code", "PRECTOTCORR", "T2M", "RH2M", "WD2M", "WS2M"]

    def __init__(self, data_dir: str, timezone_offset_hours: int = -3):
        self.data_dir = Path(data_dir)
        self.local_tz = timezone(timedelta(hours=timezone_offset_hours))
        self.timezone_offset_hours = timezone_offset_hours
        self._stations_cache: Optional[pd.DataFrame] = None
        self._year_cache: Dict[int, pd.DataFrame] = {}

    def _load_stations(self) -> pd.DataFrame:
        if self._stations_cache is None:
            path = self.data_dir / "stations.parquet"
            if path.exists():
                self._stations_cache = pd.read_parquet(path)
            else:
                self._stations_cache = pd.DataFrame(
                    columns=["station_code", "station_name", "latitude", "longitude", "altitude", "region", "uf"]
                )
        return self._stations_cache

    def _available_years(self) -> List[int]:
        if not self.data_dir.exists():
            return []
        return sorted(int(p.stem) for p in self.data_dir.glob("*.parquet") if p.stem.isdigit())

    def _load_year(self, year: int) -> pd.DataFrame:
        if year not in self._year_cache:
            path = self.data_dir / f"{year}.parquet"
            if path.exists():
                self._year_cache[year] = pd.read_parquet(path)
            else:
                self._year_cache[year] = pd.DataFrame(columns=self.DATA_COLUMNS)
        return self._year_cache[year]

    def _load_years(self, start_date: Optional[date], end_date: Optional[date]) -> pd.DataFrame:
        years = (
            range(start_date.year, end_date.year + 1)
            if start_date is not None and end_date is not None
            else self._available_years()
        )
        frames = [self._load_year(y) for y in years]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=self.DATA_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def discover_station_groups(self) -> Dict[str, Dict[str, object]]:
        groups: Dict[str, Dict[str, object]] = {}
        for _, row in self._load_stations().iterrows():
            station_code = row.get("station_code")
            station_key = station_code or f"{row.get('station_name', 'UNKNOWN')}|{row.get('latitude')}|{row.get('longitude')}"
            groups[str(station_key)] = {
                "station_code": station_code,
                "station_name": row.get("station_name"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "altitude": row.get("altitude"),
                "region": row.get("region"),
                "uf": row.get("uf"),
            }
        return groups

    def load_station_data(self, station_group: Dict[str, object], start_date: Optional[date] = None, end_date: Optional[date] = None) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["dt_local", "date_key", "hr", "T2M", "RH2M", "WS2M", "WD2M", "PRECTOTCORR"])
        df = self._load_years(start_date, end_date)
        if df.empty:
            return empty
        scoped = df.loc[df["station_code"] == station_group.get("station_code")].copy()
        if scoped.empty:
            return empty
        scoped["dt_local"] = scoped["dt_utc"].dt.tz_convert(self.local_tz)
        scoped["date_key"] = scoped["dt_local"].dt.strftime("%Y%m%d")
        scoped["hr"] = scoped["dt_local"].dt.hour.astype(str)
        scoped = scoped.drop_duplicates(subset=["dt_local"], keep="first").sort_values("dt_local")
        return scoped[["dt_local", "date_key", "hr", "T2M", "RH2M", "WS2M", "WD2M", "PRECTOTCORR"]]

    def find_candidates(
        self,
        target_lat: float,
        target_lon: float,
        start_date: date,
        end_date: date,
        radius_km: float,
        required_hourly: List[str],
        needs_daily_precip: bool,
    ) -> List[Dict[str, object]]:
        expected_hours = ((end_date - start_date).days + 1) * 24
        expected_days = (end_date - start_date).days + 1
        candidates = []

        for _, station in self.discover_station_groups().items():
            lat = station.get("latitude")
            lon = station.get("longitude")
            if lat is None or lon is None:
                continue
            distance = haversine_km(target_lat, target_lon, float(lat), float(lon))
            if distance > radius_km:
                continue

            df = self.load_station_data(station, start_date=start_date, end_date=end_date)
            if df.empty:
                continue

            mask = (df["dt_local"].dt.date >= start_date) & (df["dt_local"].dt.date <= end_date)
            scoped = df.loc[mask].copy()
            if scoped.empty:
                continue

            coverage_hours = int(scoped["dt_local"].nunique())
            coverage_ratio = coverage_hours / expected_hours if expected_hours else 0.0

            start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=self.local_tz)
            end_dt = datetime.combine(end_date, datetime.max.time().replace(hour=23, minute=0, second=0, microsecond=0), tzinfo=self.local_tz)
            full_idx = pd.date_range(start=start_dt, end=end_dt, freq="h", tz=self.local_tz)
            base = pd.DataFrame({"dt_local": full_idx, "date_key": full_idx.strftime("%Y%m%d")})
            base = base.merge(
                scoped[[c for c in ["dt_local", "PRECTOTCORR", "T2M", "RH2M", "WS2M", "WD2M"] if c in scoped.columns]],
                how="left",
                on="dt_local",
            )

            available_vars = []
            missing_cells = 0
            total_cells = 0
            for p in required_hourly:
                if p in base.columns:
                    not_null_count = int(base[p].notna().sum())
                    if not_null_count > 0:
                        available_vars.append(p)
                    total_cells += len(base)
                    missing_cells += int(base[p].isna().sum())

            day_missing_mask = pd.Series(False, index=base.index)
            for p in required_hourly:
                if p in base.columns:
                    day_missing_mask = day_missing_mask | base[p].isna()
            missing_days = sorted(base.loc[day_missing_mask, "date_key"].astype(str).unique().tolist())

            precip_missing_days = 0
            if needs_daily_precip:
                if "PRECTOTCORR" in base.columns:
                    daily = base.groupby(base["dt_local"].dt.strftime("%Y%m%d"))["PRECTOTCORR"].sum(min_count=1)
                    full_index = pd.date_range(start=start_date, end=end_date, freq="D").strftime("%Y%m%d")
                    daily = daily.reindex(full_index)
                    precip_missing_days = int(daily.isna().sum())
                    missing_days = sorted(set(missing_days).union(set(daily[daily.isna()].index.astype(str).tolist())))
                else:
                    precip_missing_days = expected_days
                    missing_days = sorted(set(missing_days).union(set(pd.date_range(start=start_date, end=end_date, freq="D").strftime("%Y%m%d").tolist())))

            missing_ratio = (missing_cells / total_cells) if total_cells else 1.0
            required_count = len(required_hourly) + (1 if needs_daily_precip else 0)
            available_count = len(available_vars) + (1 if needs_daily_precip and precip_missing_days < expected_days else 0)

            candidates.append(
                {
                    "station_code": station.get("station_code"),
                    "station_name": station.get("station_name"),
                    "distance_km": round(distance, 3),
                    "latitude": lat,
                    "longitude": lon,
                    "coverage_hours": coverage_hours,
                    "expected_hours": expected_hours,
                    "coverage_ratio": round(coverage_ratio, 6),
                    "available_variables": sorted(available_vars),
                    "available_required_count": available_count,
                    "required_variable_count": required_count,
                    "missing_ratio": round(missing_ratio, 6),
                    "precip_missing_days": precip_missing_days,
                    "missing_days": missing_days,
                    "missing_day_count": len(missing_days),
                    "_data": scoped,
                    "_station": station,
                }
            )

        # Priority: coverage, variable completeness, fewer missing, shorter distance.
        candidates.sort(
            key=lambda c: (
                -c["coverage_ratio"],
                -(c["available_required_count"] / c["required_variable_count"] if c["required_variable_count"] else 0),
                c["missing_ratio"],
                c["distance_km"],
            )
        )
        return candidates


def _init_expected_hourly_grid(start_date: date, end_date: date, local_tz: timezone) -> pd.DataFrame:
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=local_tz)
    end_dt = datetime.combine(end_date, datetime.max.time().replace(hour=23, minute=0, second=0, microsecond=0), tzinfo=local_tz)
    idx = pd.date_range(start=start_dt, end=end_dt, freq="h", tz=local_tz)
    return pd.DataFrame({"dt_local": idx, "date_key": idx.strftime("%Y%m%d"), "hr": idx.hour.astype(str)})


def _build_outputs_from_hourly_frame(
    frame: pd.DataFrame,
    hourly_req: List[str],
    needs_daily: bool,
    needs_app: bool,
    daily_req: List[str],
    apply_precip_filter: bool,
    precip_threshold: float,
    precip_filter_date_keys: Optional[set] = None,
) -> Tuple[List[Dict[str, object]], Dict[str, Dict[str, object]]]:
    hourly_records: List[Dict[str, object]] = []
    daily_storage: Dict[str, Dict[str, object]] = {}

    for _, row in frame.iterrows():
        date_key = str(row["date_key"])
        hr = str(row["hr"])
        rec = {"date_key": date_key, "hr": hr}
        for p in hourly_req:
            val = row.get(p)
            rec[p] = None if pd.isna(val) else float(val)
        if "PRECTOTCORR" in frame.columns:
            pval = row.get("PRECTOTCORR")
            rec["PRECTOTCORR"] = None if pd.isna(pval) else float(pval)
        hourly_records.append(rec)

        if needs_daily or needs_app:
            day_map = daily_storage.setdefault(date_key, {})
            for p in hourly_req:
                day_map.setdefault(p, [])
                val = rec[p]
                if val is not None and val != -999:
                    day_map[p].append(val)

    if daily_req and (needs_daily or needs_app):
        if "PRECTOTCORR" in frame.columns:
            grouped = frame.groupby("date_key")["PRECTOTCORR"].sum(min_count=1)
            for dt_key, val in grouped.items():
                day_map = daily_storage.setdefault(str(dt_key), {})
                if pd.isna(val):
                    day_map["PRECTOTCORR"] = None
                else:
                    final_val = float(val)
                    date_eligible = precip_filter_date_keys is None or str(dt_key) in precip_filter_date_keys
                    if apply_precip_filter and date_eligible and final_val < precip_threshold:
                        final_val = 0.0
                    day_map["PRECTOTCORR"] = final_val

        # Ensure all dates in range exist even if completely missing.
        for dt_key in frame["date_key"].astype(str).unique().tolist():
            daily_storage.setdefault(dt_key, {})
            if "PRECTOTCORR" in daily_req:
                daily_storage[dt_key].setdefault("PRECTOTCORR", None)

    return hourly_records, daily_storage


def _build_from_nasa_only(
    nasa: NasaPowerProvider,
    lat: float,
    lon: float,
    start_date: date,
    end_date: date,
    community: str,
    tstd: str,
    hourly_req: List[str],
    daily_req: List[str],
    apply_precip_filter: bool,
    precip_threshold: float,
    needs_daily: bool,
    needs_app: bool,
) -> WeatherBuildResult:
    hourly_df = nasa.fetch_hourly(lat, lon, start_date, end_date, community, hourly_req, tstd=tstd)
    hourly_records: List[Dict[str, object]] = []
    daily_storage: Dict[str, Dict[str, object]] = {}

    for _, row in hourly_df.iterrows():
        date_key = str(row["date_key"])
        hr = str(row["hr"])
        rec = {"date_key": date_key, "hr": hr}
        for p in hourly_req:
            val = row.get(p)
            rec[p] = None if pd.isna(val) else float(val)
        hourly_records.append(rec)

        if needs_daily or needs_app:
            day_map = daily_storage.setdefault(date_key, {})
            for p in hourly_req:
                day_map.setdefault(p, [])
                val = rec[p]
                if val is not None and val != -999:
                    day_map[p].append(val)

    if daily_req and (needs_daily or needs_app):
        daily_df = nasa.fetch_daily(lat, lon, start_date, end_date, community, daily_req, tstd=tstd)
        for _, row in daily_df.iterrows():
            date_key = str(row["date_key"])
            day_map = daily_storage.setdefault(date_key, {})
            for p in daily_req:
                val = row.get(p)
                if val is None or pd.isna(val):
                    day_map[p] = None
                else:
                    out_val = float(val)
                    if apply_precip_filter and p == "PRECTOTCORR" and out_val < precip_threshold:
                        out_val = 0.0
                    day_map[p] = out_val

    metadata = {
        "primary_source": "NASA_POWER",
        "timezone": tstd,
        "gap_fill_applied": False,
        "filled_records": [],
        "candidate_stations": [],
        "selection_reason": "NASA only mode or INMET unavailable",
    }
    return WeatherBuildResult(daily_storage=daily_storage, hourly_records=hourly_records, metadata=metadata)


def build_weather_dataset(
    lat: float,
    lon: float,
    start_date: date,
    end_date: date,
    selected_params: Dict[str, bool],
    community: str,
    tstd: str,
    out_daily: bool,
    out_hourly: bool,
    enable_app_format: bool,
    apply_precip_filter: bool,
    precip_threshold: float,
    source_strategy: str,
    inmet_radius_km: float,
    inmet_gap_fill: bool,
    inmet_data_dir: str,
    preferred_inmet_station: str = "",
    timezone_offset_hours: int = -3,
    ssl_verify: bool = False,
) -> WeatherBuildResult:
    hourly_req = [p for p in HOURLY_VARIABLES if selected_params.get(p)]
    daily_req = [p for p in DAILY_VARIABLES if selected_params.get(p)]

    nasa = NasaPowerProvider(ssl_verify=ssl_verify)

    if source_strategy == "NASA only":
        return _build_from_nasa_only(
            nasa,
            lat,
            lon,
            start_date,
            end_date,
            community,
            tstd,
            hourly_req,
            daily_req,
            apply_precip_filter,
            precip_threshold,
            out_daily,
            enable_app_format,
        )

    inmet = InmetProvider(inmet_data_dir, timezone_offset_hours=timezone_offset_hours)
    candidates = inmet.find_candidates(
        target_lat=lat,
        target_lon=lon,
        start_date=start_date,
        end_date=end_date,
        radius_km=inmet_radius_km,
        required_hourly=hourly_req,
        needs_daily_precip=bool(daily_req),
    )

    preferred_query = (preferred_inmet_station or "").strip().upper()
    if preferred_query:
        matched_candidates = [
            c
            for c in candidates
            if preferred_query in str(c.get("station_code", "")).upper()
            or preferred_query in str(c.get("station_name", "")).upper()
        ]
        if matched_candidates:
            candidates = matched_candidates

    candidate_meta = [
        {
            "station_code": c["station_code"],
            "station_name": c["station_name"],
            "distance_km": c["distance_km"],
            "coverage_ratio": c["coverage_ratio"],
            "missing_ratio": c["missing_ratio"],
            "available_variables": c["available_variables"],
            "missing_day_count": c.get("missing_day_count", 0),
            "missing_days": ", ".join(c.get("missing_days", [])[:12]),
        }
        for c in candidates
    ]

    if not candidates:
        result = _build_from_nasa_only(
            nasa,
            lat,
            lon,
            start_date,
            end_date,
            community,
            tstd,
            hourly_req,
            daily_req,
            apply_precip_filter,
            precip_threshold,
            out_daily,
            enable_app_format,
        )
        result.metadata["candidate_stations"] = candidate_meta
        result.metadata["selection_reason"] = f"No INMET station found within {inmet_radius_km:.1f} km"
        return result

    best = candidates[0]
    required_ok = best["available_required_count"] >= best["required_variable_count"]

    # A station that is missing one or more variables entirely (e.g. a wind-direction
    # sensor outage for a whole month) would otherwise cause Auto mode to discard the
    # station's otherwise-complete data for every variable and fall back to NASA POWER
    # across the board. If gap-fill is enabled, the missing variable(s) can be sourced
    # from NASA POWER individually instead, so still treat the station as usable as long
    # as it has at least some real coverage.
    if not required_ok and inmet_gap_fill and best["available_required_count"] > 0:
        required_ok = True

    if source_strategy == "Auto" and not required_ok:
        result = _build_from_nasa_only(
            nasa,
            lat,
            lon,
            start_date,
            end_date,
            community,
            tstd,
            hourly_req,
            daily_req,
            apply_precip_filter,
            precip_threshold,
            out_daily,
            enable_app_format,
        )
        result.metadata["candidate_stations"] = candidate_meta
        result.metadata["selection_reason"] = "INMET best station missing required variables; fallback to NASA"
        return result

    base_grid = _init_expected_hourly_grid(start_date, end_date, inmet.local_tz)
    station_df = best["_data"].copy()
    merged = base_grid.merge(
        station_df[["dt_local", "T2M", "RH2M", "WS2M", "WD2M", "PRECTOTCORR"]],
        how="left",
        on="dt_local",
    )

    # Primary source attribution starts as INMET for non-missing values.
    filled_records: List[Dict[str, object]] = []

    if inmet_gap_fill:
        need_gap_vars = []
        for p in hourly_req:
            if p in merged.columns and bool(merged[p].isna().any()):
                need_gap_vars.append(p)

        if need_gap_vars:
            nasa_hourly_utc = nasa.fetch_hourly(lat, lon, start_date, end_date, community, need_gap_vars, tstd="UTC")
            if not nasa_hourly_utc.empty:
                local_tz = inmet.local_tz
                nasa_hourly_utc["dt_local"] = nasa_hourly_utc["dt"].apply(lambda d: d.replace(tzinfo=timezone.utc).astimezone(local_tz))
                nasa_map = nasa_hourly_utc.set_index("dt_local")

                for p in need_gap_vars:
                    if p not in nasa_map.columns:
                        continue
                    missing_mask = merged[p].isna()
                    for idx in merged.index[missing_mask]:
                        dt_local = merged.at[idx, "dt_local"]
                        if dt_local not in nasa_map.index:
                            continue
                        fill_val = nasa_map.at[dt_local, p]
                        if pd.isna(fill_val):
                            continue
                        merged.at[idx, p] = float(fill_val)
                        filled_records.append(
                            {
                                "timestamp_local": dt_local.isoformat(),
                                "variable": p,
                                "source": "NASA_POWER",
                            }
                        )

        if daily_req and "PRECTOTCORR" in daily_req:
            daily_series = merged.groupby("date_key")["PRECTOTCORR"].sum(min_count=1)
            missing_daily = daily_series[daily_series.isna()].index.tolist()
            if missing_daily:
                nasa_daily = nasa.fetch_daily(lat, lon, start_date, end_date, community, ["PRECTOTCORR"], tstd=tstd)
                if not nasa_daily.empty:
                    nasa_daily_map = dict(zip(nasa_daily["date_key"].astype(str), nasa_daily["PRECTOTCORR"]))
                    for dkey in missing_daily:
                        fill_val = nasa_daily_map.get(str(dkey))
                        if fill_val is None or pd.isna(fill_val):
                            continue
                        day_mask = merged["date_key"].astype(str) == str(dkey)
                        first_idx = merged.index[day_mask][0]
                        merged.at[first_idx, "PRECTOTCORR"] = float(fill_val)
                        filled_records.append(
                            {
                                "timestamp_local": f"{dkey}T00:00:00",
                                "variable": "PRECTOTCORR",
                                "source": "NASA_POWER",
                            }
                        )

    # Days whose daily PRECTOTCORR total was filled in from NASA POWER (rather than
    # measured by the INMET station) — the rainfall filter is documented to apply
    # only to NASA POWER values, so it must still apply to these even when INMET
    # is the primary source for the rest of the period.
    nasa_filled_precip_dates = {
        rec["timestamp_local"].split("T")[0]
        for rec in filled_records
        if rec.get("variable") == "PRECTOTCORR"
    }

    hourly_records, daily_storage = _build_outputs_from_hourly_frame(
        merged,
        hourly_req,
        out_daily,
        enable_app_format,
        daily_req,
        apply_precip_filter,
        precip_threshold,
        precip_filter_date_keys=nasa_filled_precip_dates,
    )

    metadata = {
        "primary_source": "INMET",
        "station": {
            "station_code": best["station_code"],
            "station_name": best["station_name"],
            "distance_km": best["distance_km"],
            "latitude": best["latitude"],
            "longitude": best["longitude"],
        },
        "timezone": f"UTC{timezone_offset_hours:+03d}:00",
        "gap_fill_applied": bool(filled_records),
        "filled_records": filled_records,
        "candidate_stations": candidate_meta,
        "selection_reason": "Best ranked station selected by coverage, completeness, missing ratio, and distance",
    }

    return WeatherBuildResult(daily_storage=daily_storage, hourly_records=hourly_records, metadata=metadata)
