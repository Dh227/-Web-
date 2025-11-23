import json
import logging
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Smart Trip Backend", version="0.1.0")

# 允许本地调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # 允许任何来源（包括 file:// 打开的页面）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()
AMAP_KEY = os.getenv("AMAP_KEY")

if not AMAP_KEY:
    raise RuntimeError("AMAP_KEY is not set. Create a .env file with AMAP_KEY=<your_webservice_key>")

# 指向 frontend/users.db
# 假设 backend/fastapi_app.py, frontend/users.db
BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = BASE_DIR / "frontend" / "users.db"

logging.basicConfig(level=logging.INFO)


def _parse_lnglat(value: str) -> Tuple[float, float]:
    lng, lat = value.split(",")
    return float(lng), float(lat)


def _haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lng1, lat1 = a
    lng2, lat2 = b
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    x = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(x))


def _parse_time_to_minutes(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        h, m = value.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _minutes_to_str(value: int) -> str:
    value = max(0, int(value))
    h = value // 60
    m = value % 60
    return f"{h:02d}:{m:02d}"


def _travel_minutes(loc_a: Optional[str], loc_b: Optional[str], avg_speed_kmh: float) -> int:
    if not loc_a or not loc_b:
        return 20
    try:
        a = _parse_lnglat(loc_a)
        b = _parse_lnglat(loc_b)
    except Exception:
        return 20
    distance_m = _haversine(a, b)
    if avg_speed_kmh <= 0:
        avg_speed_kmh = 25
    minutes = (distance_m / 1000) / avg_speed_kmh * 60
    return int(max(8, math.ceil(minutes)))

class RouteRequest(BaseModel):
    origin: str        # 例: "116.434307,39.90909"
    destination: str   # 例: "116.434446,39.90816"

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/route/driving/basic")
def driving_route_basic(data: RouteRequest):
    url = "https://restapi.amap.com/v5/direction/driving"
    params = {
        "key": AMAP_KEY,
        "origin": data.origin,
        "destination": data.destination,
        "strategy": 32,            # 高德推荐
        "show_fields": "cost,navi,polyline" # 返回耗时/动作等
    }
    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data
# ======== 景点搜索（POI） ========

class PoiTextReq(BaseModel):
    keywords: str              # 关键词：如“景点”“博物馆”“长城”等
    city: Optional[str] = None # 城市代码（citycode，如北京010；不填则全国）
    types: Optional[str] = None # POI 类型编码，可先留空
    page_size: int = 10
    page_num: int = 1


class GeoGuessReq(BaseModel):
    query: str                 # 地点名称/地址
    city: Optional[str] = None # 可选城市/行政区，提升召回准确率


@app.post("/geo/guess")
def geo_guess(req: GeoGuessReq):
    """
    给地点名称，返回第一条地理编码坐标（用于前端补全缺失坐标）
    """
    if not req.query:
        return {"status": "0", "info": "query is required"}
    url = "https://restapi.amap.com/v3/geocode/geo"
    params = {
        "key": AMAP_KEY,
        "address": req.query,
        "city": req.city or "",
        "batch": "false",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
    except Exception as exc:
        logging.exception("geo guess failed: %s", exc)
        return {"status": "0", "info": "geocode error"}
    if not data or data.get("status") != "1" or not data.get("geocodes"):
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "no result"}
    geo = data["geocodes"][0]
    return {
        "status": "1",
        "location": geo.get("location"),
        "formatted_address": geo.get("formatted_address") or req.query,
        "adcode": geo.get("adcode"),
        "province": geo.get("province"),
        "city": geo.get("city") or geo.get("province"),
    }

@app.post("/poi/search")
def poi_search(req: PoiTextReq):
    """
    文本检索：根据关键词（可选城市/类型）搜索景点
    AMap: /v5/place/text
    """
    url = "https://restapi.amap.com/v5/place/text"
    params = {
        "key": AMAP_KEY,
        "keywords": req.keywords,
        "page_size": req.page_size,
        "page_num": req.page_num
    }
    if req.city:
        params["city"] = req.city
    if req.types:
        params["types"] = req.types

    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data


class PoiAroundReq(BaseModel):
    location: str              # "lng,lat"
    radius: int = 3000         # 半径，单位米（0~50000），默认 3km
    types: Optional[str] = None
    page_size: int = 10
    page_num: int = 1

@app.post("/poi/around")
def poi_around(req: PoiAroundReq):
    """
    周边检索：以某个点为中心搜索周边景点
    AMap: /v5/place/around
    """
    url = "https://restapi.amap.com/v5/place/around"
    params = {
        "key": AMAP_KEY,
        "location": req.location,
        "radius": req.radius,
        "page_size": req.page_size,
        "page_num": req.page_num
    }
    if req.types:
        params["types"] = req.types

    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data


# ======== 天气预报 ========
@app.get("/weather/forecast")
def weather_forecast(location: Optional[str] = None, city: Optional[str] = None):
    """
    根据坐标（优先）或城市名称获取未来几天天气
    """
    location = (location or "").strip()
    city = (city or "").strip()
    adcode = None
    resolved_name = None
    if location:
        adcode, resolved_name = _resolve_adcode_from_location(location)
    if not adcode and city:
        adcode, resolved_name = _resolve_adcode_from_text(city)
    if not adcode:
        return {"status": "0", "info": "无法解析目的地位置，请重新输入"}

    url = "https://restapi.amap.com/v3/weather/weatherInfo"
    params = {
        "key": AMAP_KEY,
        "city": adcode,
        "extensions": "all",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as exc:
        logging.exception("AMap weather request failed: %s", exc)
        return {"status": "0", "info": "天气服务暂时不可用，请稍后再试"}

    if data.get("status") != "1":
        return {
            "status": "0",
            "info": data.get("info") if isinstance(data, dict) else "AMap weather error",
            "debug": {"url": resp.url if 'resp' in locals() else url}
        }

    forecasts_raw = data.get("forecasts") or []
    casts = []
    city_name = resolved_name
    report_time = None
    if forecasts_raw:
        first = forecasts_raw[0]
        report_time = first.get("reporttime")
        city_name = city_name or first.get("city")
        adcode = first.get("adcode") or adcode
        for cast in first.get("casts") or []:
            casts.append({
                "date": cast.get("date"),
                "week": cast.get("week"),
                "day_weather": cast.get("dayweather"),
                "night_weather": cast.get("nightweather"),
                "day_temp": cast.get("daytemp"),
                "night_temp": cast.get("nighttemp"),
                "day_wind": cast.get("daywind"),
                "night_wind": cast.get("nightwind"),
                "day_power": cast.get("daypower"),
                "night_power": cast.get("nightpower"),
            })

    return {
        "status": "1",
        "city": city_name or city,
        "adcode": adcode,
        "report_time": report_time,
        "forecasts": casts,
    }


def _recall_pois(city: Optional[str], keywords: str, types: Optional[str] = None, page_size: int = 20) -> Dict[str, Any]:
    url = "https://restapi.amap.com/v5/place/text"
    params = {
        "key": AMAP_KEY,
        "keywords": keywords,
        "page_size": page_size,
        "page_num": 1
    }
    if city:
        params["city"] = city
    if types:
        params["types"] = types
    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap recall request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        raise RuntimeError(data.get("info") if isinstance(data, dict) else "AMap error")
    return data


# ======== 沿途景点提取 ========
class SightsAlongReq(BaseModel):
    path: List[str]                         # 折线坐标序列 ["lng,lat", ...]，需按路线顺序
    origin: Optional[str] = None
    destination: Optional[str] = None
    mode: Optional[str] = None
    radius: int = 1500                      # 每个采样点的检索半径（米）
    max_samples: int = 8                    # 最多取多少个采样点
    per_sample_limit: int = 5               # 每个采样点最多保留多少个景点
    dedup_distance: int = 600               # 景点间的最小距离（米），用于去重
    types: str = "110000|160100|160300|160200|150100|150200"


def _safe_parse_point(value: Optional[str]) -> Optional[Tuple[float, float]]:
    if not value:
        return None
    try:
        lng, lat = value.split(",")
        return float(lng), float(lat)
    except Exception:
        return None


def _downsample_path(points: List[str], limit: int) -> List[str]:
    cleaned = [p for p in points if p and "," in p]
    if len(cleaned) <= limit:
        return cleaned
    step = max(1, len(cleaned) // limit)
    sample = []
    idx = 0
    while idx < len(cleaned) and len(sample) < limit:
        sample.append(cleaned[idx])
        idx += step
    if sample[-1] != cleaned[-1]:
        sample.append(cleaned[-1])
    return sample


def _resolve_adcode_from_location(location: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Reverse geocode a "lng,lat" string and return (adcode, readable_name).
    """
    if not location or "," not in location:
        return None, None
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {
        "key": AMAP_KEY,
        "location": location,
        "extensions": "base",
        "radius": 1000,
        "roadlevel": 1,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception:
        return None, None
    if data.get("status") != "1":
        return None, None
    regeocode = data.get("regeocode") or {}
    comp = regeocode.get("addressComponent") or {}
    adcode = comp.get("adcode") or comp.get("citycode")
    display = regeocode.get("formatted_address")
    if not display:
        city = comp.get("city")
        display = city if isinstance(city, str) and city else comp.get("province")
    return adcode, display


def _resolve_adcode_from_text(city: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve a city/district text into (adcode, readable_name) using forward geocoding.
    """
    if not city:
        return None, None
    url = "https://restapi.amap.com/v3/geocode/geo"
    params = {
        "key": AMAP_KEY,
        "address": city,
        "batch": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception:
        return None, None
    if data.get("status") != "1":
        return None, None
    geocodes = data.get("geocodes") or []
    if not geocodes:
        return None, None
    info = geocodes[0]
    return info.get("adcode") or info.get("citycode"), info.get("formatted_address") or city


SCENIC_TYPE_PREFIXES = (
    "11",     # 风景名胜类
    "110",    # 景点更细
    "1601",   # 文化场馆
    "1602",   # 科教文化
    "1603",   # 展馆
    "1501",   # 旅游服务
    "1502",   # 旅游景区配套
)


def _is_scenic_type(typecode: Optional[str]) -> bool:
    if not typecode:
        return False
    return any(typecode.startswith(prefix) for prefix in SCENIC_TYPE_PREFIXES)


def _is_duplicate_spot(spots: List[Dict[str, Any]], candidate_xy: Optional[Tuple[float, float]], dedup_distance: int) -> bool:
    if not candidate_xy:
        return False
    for spot in spots:
        loc_xy = _safe_parse_point(spot.get("location"))
        if not loc_xy:
            continue
        if _haversine(loc_xy, candidate_xy) <= dedup_distance:
            return True
    return False


def _score_spot(poi: Dict[str, Any], distance_m: Optional[float]) -> float:
    rating_str = None
    biz_ext = poi.get("biz_ext") or {}
    if biz_ext.get("rating"):
        rating_str = biz_ext.get("rating")
    rating = 0.0
    try:
        if rating_str:
            rating = float(rating_str)
    except (TypeError, ValueError):
        rating = 0.0
    dist_km = (distance_m or 0) / 1000
    distance_score = max(0, 5 - dist_km)  # 越近越好
    popularity = 0.0
    try:
        if poi.get("importance"):
            popularity = float(poi.get("importance"))
    except (TypeError, ValueError):
        popularity = 0.0
    return rating * 2 + distance_score + popularity


@app.post("/route/sights-along")
def sights_along_route(req: SightsAlongReq):
    """
    沿途景点检索：沿路线对多个采样点执行 place/around，汇总去重后返回
    """
    if not req.path or len(req.path) < 2:
        return {"status": "0", "info": "need polyline path"}

    samples = _downsample_path(req.path, req.max_samples)
    if not samples:
        return {"status": "0", "info": "invalid path data"}
    all_spots: List[Dict[str, Any]] = []
    seen_ids = set()

    for idx, point in enumerate(samples):
        params = {
            "key": AMAP_KEY,
            "location": point,
            "radius": req.radius,
            "types": req.types,
            "page_size": req.per_sample_limit,
            "page_num": 1,
            "sortrule": "weight"  # 综合排序
        }
        r = requests.get("https://restapi.amap.com/v5/place/around", params=params, timeout=20)
        logging.info("AMap request(sights): %s", r.url)
        data = r.json()
        if not data or data.get("status") != "1":
            logging.warning("sights fetch failed: %s", data.get("info"))
            continue

        pois = data.get("pois") or []
        sample_xy = _safe_parse_point(point)
        for poi in pois:
            poi_id = poi.get("id") or f"{poi.get('name')}_{poi.get('location')}"
            if poi_id in seen_ids:
                continue
            loc_str = poi.get("location")
            loc_xy = _safe_parse_point(loc_str)
            if _is_duplicate_spot(all_spots, loc_xy, req.dedup_distance):
                continue
            if not _is_scenic_type(poi.get("typecode")):
                continue

            dist_val = _haversine(sample_xy, loc_xy) if sample_xy and loc_xy else None
            biz_ext = poi.get("biz_ext") or {}
            highlight_parts = []
            if biz_ext.get("rating"):
                highlight_parts.append(f"评分 {biz_ext['rating']}")
            if biz_ext.get("cost"):
                highlight_parts.append(f"门票约 {biz_ext['cost']}")
            if poi.get("tag"):
                highlight_parts.append(poi["tag"])
            score = _score_spot(poi, dist_val)

            spot = {
                "id": poi_id,
                "name": poi.get("name"),
                "type": poi.get("type"),
                "typecode": poi.get("typecode"),
                "address": poi.get("address"),
                "location": loc_str,
                "cityname": poi.get("cityname"),
                "adname": poi.get("adname"),
                "rating": biz_ext.get("rating"),
                "price": biz_ext.get("cost"),
                "distance_m": int(dist_val) if dist_val is not None else None,
                "sample_index": idx,
                "highlight": "，".join(highlight_parts) if highlight_parts else None,
                "source": "amap",
                "score": score
            }
            all_spots.append(spot)
            seen_ids.add(poi_id)

    if not all_spots:
        return {"status": "0", "info": "no sights found"}

    all_spots.sort(key=lambda s: s.get("score", 0), reverse=True)
    top_spots = all_spots[:15]
    for rank, spot in enumerate(top_spots, start=1):
        spot["rank"] = rank
        spot["recommended"] = (rank == 1)

    return {
        "status": "1",
        "info": "OK",
        "origin": req.origin,
        "destination": req.destination,
        "mode": req.mode,
        "samples": samples,
        "pois": top_spots
    }
# ======== 多景点顺路规划（贪心法 + 高德驾车） ========
class PlanRequest(BaseModel):
    # 起点/终点都是 "lng,lat"；waypoints 是一组中途景点（至少 1 个）
    origin: str                 # 必填，如 "116.397463,39.909187"（天安门）
    waypoints: List[str]        # 必填，["lng,lat", "lng,lat", ...]，例如若干景点
    destination: Optional[str] = None     # 选填；不填则以最后一个点为终点
    strategy: int = 32                     # 高德算路策略，默认 32=推荐
    avoid_highway: bool = False            # 示例：可以作为策略开关之一（扩展用）

def _greedy_order(origin: str, points: List[str]) -> List[str]:
    # 从起点出发，每次找最近的下一个点，直到走完
    ordered = []
    cur = origin
    remaining = points.copy()
    cur_xy = _parse_lnglat(cur)
    while remaining:
        # 找最近
        best_i, best_d = 0, float("inf")
        for i, p in enumerate(remaining):
            d = _haversine(cur_xy, _parse_lnglat(p))
            if d < best_d:
                best_d = d; best_i = i
        nxt = remaining.pop(best_i)
        ordered.append(nxt)
        cur = nxt
        cur_xy = _parse_lnglat(cur)
    return ordered

@app.post("/route/plan")
def plan_route(req: PlanRequest):
    """
    输入：起点 + 若干景点(waypoints) + 可选终点
    步骤：
      1) 用贪心近邻法把 waypoints 排成“看起来顺路”的顺序
      2) 组装高德驾车规划参数（origin -> waypoint1 -> ... -> destination）
      3) 返回：排序后的点列表 + 高德整段路线 JSON
    """
    if not req.waypoints:
        return {"status": "0", "info": "need at least one waypoint"}

    # 1) 排序（顺路）
    ordered_waypoints = _greedy_order(req.origin, req.waypoints)

    # 2) 终点：如果没给终点，就用最后一个点作为终点；waypoints 去掉最后一个
    if req.destination:
        destination = req.destination
    else:
        destination = ordered_waypoints[-1]
        ordered_waypoints = ordered_waypoints[:-1]  # 剩下的才是途经点

    # 高德 waypoints 以分号分隔
    wp_str = ";".join(ordered_waypoints) if ordered_waypoints else None

    params = {
        "key": AMAP_KEY,
        "origin": req.origin,
        "destination": destination,
        "strategy": req.strategy,
        "show_fields": "cost,navi,polyline"  # 需要折线/动作/耗时等
    }
    if wp_str:
        params["waypoints"] = wp_str

    url = "https://restapi.amap.com/v5/direction/driving"
    r = requests.get(url, params=params, timeout=30)
    logging.info("AMap request: %s", r.url)
    amap_json = r.json()
    if not amap_json or amap_json.get("status") != "1":
        return {"status": "0", "info": amap_json.get("info") if isinstance(amap_json, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}

    return {
        "status": "1",
        "info": "OK",
        "ordered_points": {
            "origin": req.origin,
            "waypoints_ordered": ordered_waypoints,
            "destination": destination
        },
        "amap_result": amap_json
    }
# ===== 驾车：常见扩展 =====

class DrivingReq(BaseModel):
    origin: str                  # "lng,lat"
    destination: str             # "lng,lat"
    strategy: int = 32           # 算路策略，默认高德推荐
    waypoints: Optional[List[str]] = None   # ["lng,lat","lng,lat",...]
    avoidpolygons: Optional[List[List[str]]] = None
    # 例如：[[ "116.39,39.90","116.40,39.90","116.40,39.91","116.39,39.91" ]]
    plate: Optional[str] = None  # 车牌（限行判断用，如 "京A12345"）
    cartype: int = 0             # 0燃油/1纯电/2插混
    ferry: int = 0               # 0可用渡轮 / 1不使用渡轮
    show_fields: str = "cost,navi,polyline,tolls,toll_distance,traffic_lights"

def _join_waypoints(points: Optional[List[str]]) -> Optional[str]:
    if not points: return None
    return ";".join(points)

def _join_avoidpolygons(polys: Optional[List[List[str]]]) -> Optional[str]:
    # 多个区域用 | 分隔；区域内顶点用 ; 分隔
    # 每个点是 "lng,lat"
    if not polys: return None
    regions = []
    for poly in polys:
        regions.append(";".join(poly))
    return "|".join(regions)

@app.post("/route/driving")
def driving_route(req: DrivingReq):
    url = "https://restapi.amap.com/v5/direction/driving"
    params = {
        "key": AMAP_KEY,
        "origin": req.origin,
        "destination": req.destination,
        "strategy": req.strategy,
        "cartype": req.cartype,
        "ferry": req.ferry,
        "show_fields": req.show_fields
    }
    wp = _join_waypoints(req.waypoints)
    if wp: params["waypoints"] = wp

    ap = _join_avoidpolygons(req.avoidpolygons)
    if ap: params["avoidpolygons"] = ap

    if req.plate:
        params["plate"] = req.plate

    r = requests.get(url, params=params, timeout=30)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data
# ===== 步行 =====
class WalkingReq(BaseModel):
    origin: str
    destination: str
    isindoor: int = 0            # 1需要室内算路
    show_fields: str = "cost,navi,polyline"

@app.post("/route/walking")
def walking_route(req: WalkingReq):
    url = "https://restapi.amap.com/v5/direction/walking"
    params = {
        "key": AMAP_KEY,
        "origin": req.origin,
        "destination": req.destination,
        "isindoor": req.isindoor,
        "show_fields": req.show_fields
    }
    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data

# ===== 骑行 =====
class BicyclingReq(BaseModel):
    origin: str
    destination: str
    show_fields: str = "cost,navi,polyline"

@app.post("/route/bicycling")
def bicycling_route(req: BicyclingReq):
    url = "https://restapi.amap.com/v5/direction/bicycling"
    params = {
        "key": AMAP_KEY,
        "origin": req.origin,
        "destination": req.destination,
        "show_fields": req.show_fields
    }
    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data

# ===== 电动车（骑行） =====
class EbikeReq(BaseModel):
    origin: str
    destination: str
    show_fields: str = "cost,navi,polyline"

@app.post("/route/ebike")
def ebike_route(req: EbikeReq):
    url = "https://restapi.amap.com/v5/direction/electrobike"
    params = {
        "key": AMAP_KEY,
        "origin": req.origin,
        "destination": req.destination,
        "show_fields": req.show_fields
    }
    r = requests.get(url, params=params, timeout=20)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data

# ===== 公交（同城/跨城） =====
class TransitReq(BaseModel):
    origin: str
    destination: str
    city1: str                    # 起点 citycode，如 北京=010
    city2: str                    # 终点 citycode
    strategy: int = 0             # 0推荐/1最经济/2最少换乘/3最少步行/8时间最短 等
    AlternativeRoute: int = 5     # 1~10 返回备选数
    nightflag: int = 0            # 1考虑夜班车
    show_fields: str = "cost,polyline"

@app.post("/route/transit")
def transit_route(req: TransitReq):
    url = "https://restapi.amap.com/v5/direction/transit/integrated"
    params = {
        "key": AMAP_KEY,
        "origin": req.origin,
        "destination": req.destination,
        "city1": req.city1,
        "city2": req.city2,
        "strategy": req.strategy,
        "AlternativeRoute": req.AlternativeRoute,
        "nightflag": req.nightflag,
        "show_fields": req.show_fields
    }
    r = requests.get(url, params=params, timeout=30)
    logging.info("AMap request: %s", r.url)
    data = r.json()
    if not data or data.get("status") != "1":
        return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}
    return data
# ===== Kimi Chat 代理 =====

KIMI_API_KEY = os.getenv("KIMI_API_KEY")
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")

class ChatMsg(BaseModel):
    role: str          # "user" | "system" | "assistant"
    content: str

class KimiChatReq(BaseModel):
    model: str = "moonshot-v1-8k"        # 可换 32k / 128k 等
    messages: List[ChatMsg]
    temperature: float = 0.7
    stream: bool = False                  # 需要流式就传 True


class ScenicSpot(BaseModel):
    name: str
    location: str
    type: Optional[str] = None
    address: Optional[str] = None
    distance_m: Optional[float] = None
    rating: Optional[str] = None
    price: Optional[str] = None
    highlight: Optional[str] = None
    cityname: Optional[str] = None
    adname: Optional[str] = None
    source: Optional[str] = None


class RouteContext(BaseModel):
    origin: str
    destination: str
    mode: str
    distance_km: Optional[float] = None
    duration_min: Optional[float] = None
    waypoints: List[str] = Field(default_factory=list)
    stats: Optional[Dict[str, Any]] = None
    scenic_spots: List[ScenicSpot] = Field(default_factory=list)


class KimiRouteReq(BaseModel):
    question: str
    context: RouteContext
    model: str = "moonshot-v1-8k"
    temperature: float = 0.6


class TripBriefingReq(BaseModel):
    messages: List[ChatMsg]
    model: str = "moonshot-v1-8k"
    temperature: float = 0.4


class POIRankingReq(BaseModel):
    city: str
    theme: Optional[str] = None
    keywords: Optional[str] = None
    user_profile: Optional[str] = None
    priority: Literal["balanced", "quality", "hot"] = "balanced"
    limit: int = 6
    types: Optional[str] = None


class MealBreak(BaseModel):
    name: str
    earliest: str  # "HH:MM"
    latest: str    # "HH:MM"
    duration: int  # minutes


# ======== 用户搜索 API ========

class UserSearchReq(BaseModel):
    keyword: Optional[str] = None
    limit: int = 50

@app.post("/users/search")
def search_users(req: UserSearchReq):
    """
    简单的用户搜索接口，用于前端按需加载用户列表
    """
    if not DATABASE_PATH.exists():
        return {"status": "0", "info": "Database not found", "users": []}

    keyword = (req.keyword or "").strip()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        if not keyword:
            # 默认返回前 limit 个
            sql = "SELECT id, username, phone, email, created_at FROM users ORDER BY id DESC LIMIT ?"
            cur.execute(sql, (req.limit,))
        else:
            # 模糊搜索
            pattern = f"%{keyword}%"
            sql = """
                SELECT id, username, phone, email, created_at 
                FROM users 
                WHERE username LIKE ? OR phone LIKE ? OR email LIKE ?
                ORDER BY id DESC LIMIT ?
            """
            cur.execute(sql, (pattern, pattern, pattern, req.limit))
        
        rows = cur.fetchall()
        users = [dict(row) for row in rows]
        return {"status": "1", "users": users}
    except Exception as e:
        logging.exception("User search failed")
        return {"status": "0", "info": str(e), "users": []}
    finally:
        conn.close()

    name: str = "午餐"
    earliest: str = "12:00"
    latest: str = "13:30"
    duration: int = 60


class ItineraryPOI(BaseModel):
    name: str
    location: Optional[str] = None
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    stay_minutes: int = 60
    cost: Optional[float] = None
    priority: int = Field(3, ge=1, le=5)
    tags: Optional[List[str]] = None


class ItineraryRequest(BaseModel):
    pois: List[ItineraryPOI]
    day_start: str = "08:00"
    day_end: str = "21:00"
    start_location: Optional[str] = None
    budget_limit: Optional[float] = None
    avg_speed_kmh: float = 25
    breaks: Optional[List[MealBreak]] = None
    locked_pois: Optional[List[str]] = None
    excluded_pois: Optional[List[str]] = None
    tempo: Literal["relax", "normal", "rush"] = "normal"
    swap_map: Optional[Dict[str, ItineraryPOI]] = None
    enforce_order: bool = False


def _build_route_context_text(ctx: RouteContext) -> str:
    mode_map = {
        "drive": "驾车",
        "driving": "驾车",
        "walk": "步行",
        "walking": "步行",
        "bike": "骑行",
        "bicycling": "骑行",
        "ebike": "电动车",
        "electrobike": "电动车",
        "transit": "公交/公共交通"
    }
    lines = []
    lines.append(f"出行方式：{mode_map.get(ctx.mode, ctx.mode)}")
    lines.append(f"起点：{ctx.origin}")
    lines.append(f"终点：{ctx.destination}")
    if ctx.waypoints:
        lines.append(f"用户设置的途经点：{' -> '.join(ctx.waypoints)}")
    if ctx.distance_km is not None:
        segment = f"总里程约 {ctx.distance_km:.1f} km"
        if ctx.duration_min is not None:
            segment += f"，预估耗时 {ctx.duration_min:.1f} 分钟"
        lines.append(segment)
    elif ctx.duration_min is not None:
        lines.append(f"预估耗时 {ctx.duration_min:.1f} 分钟")

    stats = ctx.stats or {}
    stat_parts = []
    if isinstance(stats.get("tolls"), (int, float)):
        stat_parts.append(f"收费约 {stats['tolls']} 元")
    if isinstance(stats.get("lights"), (int, float)):
        stat_parts.append(f"红绿灯约 {stats['lights']} 个")
    if stats.get("strategy") is not None:
        stat_parts.append(f"策略代码 {stats['strategy']}")
    if stats.get("cartype") is not None:
        cartype_map = {0: "燃油车", 1: "纯电", 2: "插混"}
        stat_parts.append(f"车辆类型 {cartype_map.get(stats['cartype'], stats['cartype'])}")
    if stats.get("segments"):
        stat_parts.append(f"路线包含 {stats['segments']} 个路段")
    if stat_parts:
        lines.append("路线附加参数：" + "；".join(stat_parts))

    if ctx.scenic_spots:
        lines.append("沿途候选景点：")
        for spot in ctx.scenic_spots[:10]:
            desc = f"- {spot.name}"
            if spot.type:
                desc += f"（{spot.type}）"
            if spot.distance_m is not None:
                desc += f"，距路线约 {spot.distance_m/1000:.1f} km"
            if spot.address:
                desc += f"，地址：{spot.address}"
            if spot.highlight:
                desc += f"；亮点：{spot.highlight}"
            elif spot.rating or spot.price:
                extra = []
                if spot.rating:
                    extra.append(f"评分 {spot.rating}")
                if spot.price:
                    extra.append(f"门票 {spot.price}")
                if extra:
                    desc += "；" + "，".join(extra)
            lines.append(desc)

    return "\n".join(lines)


def _parse_agent_json(content: str) -> Dict[str, Any]:
    if not content:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match:
        chunk = match.group(0)
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            return {}
    return {}

@app.post("/chat/kimi")
def chat_kimi(req: KimiChatReq):
    if not KIMI_API_KEY:
        return {"status": "0", "info": "KIMI_API_KEY not set"}

    url = f"{KIMI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": req.model,
        "messages": [m.dict() for m in req.messages],
        "temperature": req.temperature,
        "stream": req.stream,
    }

    # 非流式
    if not req.stream:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        try:
            j = r.json()
        except Exception:
            return {"status": "0", "info": "kimi error", "debug": r.text[:200]}
        # 统一一个简单响应结构
        if "choices" in j and j["choices"]:
            content = j["choices"][0]["message"]["content"]
            return {"status": "1", "content": content, "raw": j}
        return {"status": "0", "info": j.get("error", j)}

    # 流式（SSE）
    def _gen():
        with requests.post(url, headers=headers, json=payload, stream=True, timeout=300) as resp:
            for line in resp.iter_lines():
                if not line:
                    continue
                # 直接把 Kimi 的 SSE 透传回前端
                yield line + b"\n"
    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.post("/chat/kimi/route")
def chat_kimi_route(req: KimiRouteReq):
    if not KIMI_API_KEY:
        return {"status": "0", "info": "KIMI_API_KEY not set"}

    ctx_text = _build_route_context_text(req.context)
    url = f"{KIMI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    system_prompt = (
        "你是一个与高德路线打通的旅行助理，需要结合提供的路线概况和沿途景点，"
        "回答用户的问题，突出沿途亮点、玩法建议与注意事项。语气亲切，"
        "可给出 2-3 条建议，必要时提醒门票/时间/交通细节。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"{ctx_text}\n\n用户提问：{req.question}"
        }
    ]
    payload = {
        "model": req.model,
        "messages": messages,
        "temperature": req.temperature,
        "stream": False
    }

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    try:
        j = r.json()
    except Exception:
        return {"status": "0", "info": "kimi error", "debug": r.text[:200]}

    if "choices" in j and j["choices"]:
        content = j["choices"][0]["message"]["content"]
        return {"status": "1", "content": content, "raw": j, "context_summary": ctx_text}
    return {"status": "0", "info": j.get("error", j)}


@app.post("/agent/trip-briefing")
def trip_briefing_agent(req: TripBriefingReq):
    if not KIMI_API_KEY:
        return {"status": "0", "info": "KIMI_API_KEY not set"}

    system_prompt = (
        "你是一个中文旅行需求访谈 Agent，需要通过多轮引导式问答，"
        "抽取用户的出行信息。请始终以 JSON 格式回复："
        '{"reply":"<给用户的口语化回答>",'
        '"ask":"<下一问，若已全部明确则简短总结>",'
        '"slots":{"trip_days":"","budget":"","pace":"","themes":[],"depart_window":"","return_window":"","traveler_profile":"","notes":""},'
        '"confidence":0.0}'
        "。slots 中未知项保持空字符串或空数组；reply 应总结当前理解，ask 用于下一轮追问。"
    )
    payload = {
        "model": req.model,
        "temperature": req.temperature,
        "stream": False,
        "messages": [{"role": "system", "content": system_prompt}] + [m.dict() for m in req.messages]
    }

    r = requests.post(f"{KIMI_BASE_URL}/chat/completions", headers={
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }, json=payload, timeout=60)
    try:
        resp = r.json()
    except Exception:
        return {"status": "0", "info": "kimi error", "debug": r.text[:200]}

    if "choices" not in resp or not resp["choices"]:
        return {"status": "0", "info": resp.get("error", resp)}

    content = resp["choices"][0]["message"]["content"]
    parsed = _parse_agent_json(content)
    reply = parsed.get("reply") if isinstance(parsed, dict) else None
    ask = parsed.get("ask") if isinstance(parsed, dict) else None
    slots = parsed.get("slots") if isinstance(parsed, dict) else None
    confidence = parsed.get("confidence") if isinstance(parsed, dict) else None

    return {
        "status": "1",
        "content": content,
        "reply": reply or content,
        "ask": ask,
        "slots": slots,
        "confidence": confidence,
        "raw": resp
    }


@app.post("/agent/poi-ranking")
def poi_ranking_agent(req: POIRankingReq):
    if not KIMI_API_KEY:
        return {"status": "0", "info": "KIMI_API_KEY not set"}
    query_word = req.keywords or req.theme
    if not query_word:
        return {"status": "0", "info": "请提供 keywords 或 theme"}
    try:
        recall_json = _recall_pois(req.city, query_word, req.types, page_size=20)
    except Exception as exc:
        return {"status": "0", "info": f"召回失败: {exc}"}

    pois = recall_json.get("pois") or []
    if not pois:
        return {"status": "0", "info": "没有召回到景点"}

    norm_list: List[Dict[str, Any]] = []
    candidate_lines: List[str] = []
    for idx, poi in enumerate(pois[:20], start=1):
        biz = poi.get("biz_ext") or {}
        norm = {
            "id": poi.get("id"),
            "name": poi.get("name"),
            "type": poi.get("type"),
            "typecode": poi.get("typecode"),
            "address": poi.get("address"),
            "location": poi.get("location"),
            "cityname": poi.get("cityname"),
            "adname": poi.get("adname"),
            "tag": poi.get("tag"),
            "rating": biz.get("rating"),
            "cost": biz.get("cost"),
            "tel": poi.get("tel"),
            "importance": poi.get("importance"),
            "photos": poi.get("photos"),
            "distance": poi.get("distance"),
            "shopid": poi.get("shopinfo"),
        }
        norm_list.append(norm)
        info_parts = [
            f"类型:{norm.get('type') or '未知'}",
            f"评分:{norm.get('rating') or '无'}",
            f"区域:{norm.get('adname') or norm.get('cityname') or '未知'}",
        ]
        if norm.get("tag"):
            info_parts.append(f"标签:{norm['tag']}")
        if norm.get("cost"):
            info_parts.append(f"人均:{norm['cost']}")
        candidate_lines.append(f"{idx}. {norm['name']}｜" + "｜".join(info_parts))

    priority_prompt = {
        "balanced": "兼顾口碑与大众热度，输出多元组合",
        "quality": "优先高评分、好口碑、体验感强的地点",
        "hot": "优先社交媒体热度高、人气旺的地点"
    }[req.priority]
    user_profile = req.user_profile or "未提供（请根据主题与常见偏好给出建议）"
    theme_desc = req.theme or req.keywords or "综合玩法"
    instruction = (
        "你是一个 POI 召回/排序 Agent。"
        "请基于候选 POI 与用户画像、主题需求进行筛选，并输出 JSON："
        '{"reply":"","pois":[{"name":"","source_id":"","reason":"","score":0-10,"tags":[],"address":""}]}。'
        "source_id 对应候选列表的 id。score 用于排序，10 为最推荐。"
    )
    user_content = (
        f"城市：{req.city}\n"
        f"主题：{theme_desc}\n"
        f"用户画像：{user_profile}\n"
        f"排序偏好：{priority_prompt}\n"
        f"期望推荐数量：{req.limit}\n"
        "候选 POI 列表：\n" + "\n".join(candidate_lines)
    )

    payload = {
        "model": "moonshot-v1-8k",
        "temperature": 0.35,
        "stream": False,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": user_content}
        ]
    }
    headers = {
        "Authorization": f"Bearer {KIMI_API_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.post(f"{KIMI_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=60)
    try:
        resp = r.json()
    except Exception:
        return {"status": "0", "info": "kimi error", "debug": r.text[:200]}

    if "choices" not in resp or not resp["choices"]:
        return {"status": "0", "info": resp.get("error", resp)}

    content = resp["choices"][0]["message"]["content"]
    parsed = _parse_agent_json(content)
    ranked = []
    reply = content
    if isinstance(parsed, dict):
        reply = parsed.get("reply", content)
        ranked = parsed.get("pois") or []

    return {
        "status": "1",
        "city": req.city,
        "theme": theme_desc,
        "priority": req.priority,
        "reply": reply,
        "ranked": ranked,
        "candidates": norm_list,
        "raw": resp
    }


def _build_task_from_poi(
    poi: ItineraryPOI,
    day_start_min: int,
    day_end_min: int,
    stay_multiplier: float = 1.0,
    locked: bool = False,
    order: int = 0,
):
    open_min = _parse_time_to_minutes(poi.open_time) or day_start_min
    close_min = _parse_time_to_minutes(poi.close_time) or day_end_min
    window_start = max(day_start_min, open_min)
    window_end = min(day_end_min, close_min)
    stay = max(15, int(round(poi.stay_minutes * stay_multiplier)))
    return {
        "name": poi.name,
        "location": poi.location,
        "stay": stay,
        "window_start": window_start,
        "window_end": window_end,
        "cost": poi.cost or 0.0,
        "priority": poi.priority,
        "tags": poi.tags or [],
        "is_break": False,
        "locked": locked,
        "order": order,
    }


def _build_break_task(brk: MealBreak, day_start_min: int, day_end_min: int):
    earliest = _parse_time_to_minutes(brk.earliest) or day_start_min
    latest = _parse_time_to_minutes(brk.latest) or day_end_min
    window_start = max(day_start_min, earliest)
    window_end = min(day_end_min, latest)
    return {
        "name": brk.name,
        "location": None,
        "stay": max(15, brk.duration),
        "window_start": window_start,
        "window_end": window_end,
        "cost": 0.0,
        "priority": 6,  # ensure inserted
        "tags": ["餐食/休息"],
        "is_break": True,
        "locked": True,
    }


@app.post("/plan/itinerary")
def plan_itinerary(req: ItineraryRequest):
    if not req.pois:
        return {"status": "0", "info": "需要至少一个 POI"}

    day_start = _parse_time_to_minutes(req.day_start)
    day_end = _parse_time_to_minutes(req.day_end)
    if day_start is None or day_end is None or day_end <= day_start:
        return {"status": "0", "info": "请提供有效的起止时间，如 08:00~21:00"}

    locked_set = set(req.locked_pois or [])
    excluded_set = set(req.excluded_pois or [])
    swap_map = req.swap_map or {}
    tempo_profiles = {
        "relax": {"stay_mul": 1.2, "speed_mul": 0.85, "label": "慢节奏"},
        "normal": {"stay_mul": 1.0, "speed_mul": 1.0, "label": "均衡"},
        "rush": {"stay_mul": 0.8, "speed_mul": 1.15, "label": "紧凑"},
    }
    tempo_cfg = tempo_profiles.get(req.tempo, tempo_profiles["normal"])
    stay_multiplier = tempo_cfg["stay_mul"]

    tasks: List[Dict[str, Any]] = []
    for idx, poi in enumerate(req.pois):
        if poi.name in excluded_set:
            continue
        poi_source = swap_map.get(poi.name) or poi
        tasks.append(
            _build_task_from_poi(
                poi_source,
                day_start,
                day_end,
                stay_multiplier=stay_multiplier,
                locked=poi.name in locked_set,
                order=idx,
            )
        )

    breaks = req.breaks if req.breaks is not None else [MealBreak()]
    for brk in breaks:
        tasks.append(_build_break_task(brk, day_start, day_end))

    remaining = tasks.copy()
    schedule: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    explanations: List[str] = []

    current_time = day_start
    current_loc = req.start_location or (req.pois[0].location if req.pois and req.pois[0].location else None)
    budget_used = 0.0
    avg_speed = req.avg_speed_kmh if req.avg_speed_kmh > 0 else 25
    avg_speed = max(5, avg_speed * tempo_cfg["speed_mul"])

    def try_schedule_task(task):
        nonlocal current_time, current_loc, budget_used
        travel = 0 if task["is_break"] else _travel_minutes(current_loc, task["location"], avg_speed)
        start_time = max(current_time + travel, task["window_start"])
        finish_time = start_time + task["stay"]
        if finish_time > task["window_end"]:
            return False, travel, start_time, finish_time
        projected_budget = budget_used + task.get("cost", 0)
        if req.budget_limit is not None and projected_budget > req.budget_limit + 1e-6:
            return False, travel, start_time, finish_time
        if travel > 0 and start_time - travel > current_time:
            schedule.append({
                "type": "travel",
                "name": "移动/缓冲",
                "start": _minutes_to_str(current_time),
                "end": _minutes_to_str(start_time - travel),
                "from": current_loc,
                "to": task.get("location"),
                "notes": f"预计行驶 {travel} 分钟",
            })
        if start_time > current_time:
            current_time = start_time
        schedule.append({
            "type": "break" if task["is_break"] else "poi",
            "name": task["name"],
            "location": task.get("location"),
            "start": _minutes_to_str(start_time),
            "end": _minutes_to_str(finish_time),
            "cost": task.get("cost", 0),
            "tags": task.get("tags", []),
            "notes": "休息/餐饮" if task["is_break"] else f"停留 {task['stay']} 分钟"
        })
        current_time = finish_time
        current_loc = task["location"] or current_loc
        budget_used = projected_budget
        return True, travel, start_time, finish_time

    while remaining:
        if req.enforce_order:
            # 按输入顺序尝试
            best = remaining[0]
            success, travel, start_time, finish_time = try_schedule_task(best)
            if success:
                remaining.pop(0)
                continue
            # 不成功则跳过并记录
            remaining.pop(0)
            reason = []
            if finish_time > best["window_end"]:
                reason.append("时间窗口冲突")
            if req.budget_limit is not None and (budget_used + best.get("cost", 0)) > req.budget_limit:
                reason.append("预算不足")
            if not reason:
                reason.append("时间不足")
            skipped.append({"name": best["name"], "reason": "，".join(reason)})
            if best.get("locked"):
                explanations.append(f"锁定景点 {best['name']} 因{reason[0]}未能按顺序排入。")
            continue

        # 原有贪心
        best = None
        best_meta = None
        for task in remaining:
            travel = 0 if task["is_break"] else _travel_minutes(current_loc, task["location"], avg_speed)
            start_time = max(current_time + travel, task["window_start"])
            finish_time = start_time + task["stay"]
            if finish_time > task["window_end"]:
                continue
            projected_budget = budget_used + task.get("cost", 0)
            if req.budget_limit is not None and projected_budget > req.budget_limit + 1e-6:
                continue
            slack = task["window_end"] - finish_time
            order_bonus = max(0, 500000 - task.get("order", 0) * 1000) if req.enforce_order else 0
            score = (500000 if task.get("locked") else 0) + task["priority"] * 1000 - start_time - travel * 2 + slack * 0.1 + order_bonus
            if not best or score > best_meta["score"]:
                best = task
                best_meta = {
                    "start": start_time,
                    "finish": finish_time,
                    "travel": travel,
                    "score": score,
                    "projected_budget": projected_budget,
                }
        if not best:
            break

        remaining.remove(best)
        start = best_meta["start"]
        finish = best_meta["finish"]
        travel = best_meta["travel"]

        if travel > 0 and start - travel > current_time:
            schedule.append({
                "type": "travel",
                "name": "移动/缓冲",
                "start": _minutes_to_str(current_time),
                "end": _minutes_to_str(start - travel),
                "from": current_loc,
                "to": best.get("location"),
                "notes": f"预计行驶 {travel} 分钟",
            })
        if start > current_time:
            current_time = start

        schedule.append({
            "type": "break" if best["is_break"] else "poi",
            "name": best["name"],
            "location": best.get("location"),
            "start": _minutes_to_str(start),
            "end": _minutes_to_str(finish),
            "cost": best.get("cost", 0),
            "tags": best.get("tags", []),
            "notes": "休息/餐饮" if best["is_break"] else f"停留 {best['stay']} 分钟"
        })
        current_time = finish
        current_loc = best["location"] or current_loc
        budget_used = best_meta["projected_budget"]

    for task in remaining:
        reason = []
        if task["window_end"] <= current_time:
            reason.append("已过开放时间")
        if req.budget_limit is not None and (budget_used + task.get("cost", 0)) > req.budget_limit:
            reason.append("预算不足")
        if not reason:
            reason.append("时间不足")
        skipped.append({
            "name": task["name"],
            "reason": "，".join(reason)
        })
        if task.get("locked"):
            explanations.append(f"锁定景点 {task['name']} 因{reason[0]}未能排入，请调整时间或预算。")

    if locked_set:
        scheduled_locked = [s["name"] for s in schedule if s["type"] != "travel" and s["name"] in locked_set]
        missing = locked_set.difference(scheduled_locked)
        explanations.append(
            f"锁定景点：{'、'.join(locked_set)}，已优先安排{len(scheduled_locked)}项"
            + (f"，未排入：{'、'.join(missing)}" if missing else "")
        )
    if excluded_set:
        explanations.append(f"已排除景点：{'、'.join(excluded_set)}")
    if swap_map:
        swap_texts = [f"{old} → {getattr(swap_map[old], 'name', '新景点')}" for old in swap_map.keys()]
        explanations.append(f"替换景点：{'；'.join(swap_texts)}")
    if req.tempo != "normal":
        explanations.append(f"行程节奏调整为{tempo_cfg['label']}，停留时长与交通速度已自动放缩。")
    if req.enforce_order:
        explanations.append("已按照列表顺序安排行程，若要更顺路可关闭“按顺序”开关。")
    if breaks:
        names = [b.name for b in breaks]
        explanations.append(f"已插入休息/餐饮：{'、'.join(names)}")

    summary = {
        "day_start": _minutes_to_str(day_start),
        "day_end": _minutes_to_str(day_end),
        "start_location": req.start_location,
        "utilized_budget": round(budget_used, 1),
        "budget_limit": req.budget_limit,
        "scheduled_count": len([s for s in schedule if s["type"] != "travel"]),
        "skipped_count": len(skipped),
        "tempo_label": tempo_cfg["label"],
    }
    return {
        "status": "1",
        "schedule": schedule,
        "skipped": skipped,
        "summary": summary,
        "explain": explanations,
    }
# ===== 段间耗时统计（驾车/步行/骑行/电动车） =====

class SegmentsReq(BaseModel):
    # points 顺序为：起点, 途经1, 途经2, ..., 终点
    points: List[str]
    mode: Literal["driving", "walking", "bicycling", "electrobike"] = "driving"
    # 以下参数仅在驾车时有用
    strategy: int = 32
    cartype: int = 0
    ferry: int = 0

@app.post("/route/segments")
def route_segments(req: SegmentsReq):
    """
    对 points 内相邻两点分别调用高德路线接口，统计每段的 distance 与 duration。
    支持：driving / walking / bicycling / electrobike
    """
    if not req.points or len(req.points) < 2:
        return {"status": "0", "info": "need at least 2 points"}

    # 选择接口与默认 show_fields
    if req.mode == "driving":
        base_url = "https://restapi.amap.com/v5/direction/driving"
        extra = {"strategy": req.strategy, "cartype": req.cartype, "ferry": req.ferry}
    elif req.mode == "walking":
        base_url = "https://restapi.amap.com/v5/direction/walking"
        extra = {"isindoor": 0}
    elif req.mode == "bicycling":
        base_url = "https://restapi.amap.com/v5/direction/bicycling"
        extra = {}
    elif req.mode == "electrobike":
        base_url = "https://restapi.amap.com/v5/direction/electrobike"
        extra = {}
    else:
        return {"status": "0", "info": f"unsupported mode {req.mode}"}

    segments: List[Dict] = []
    total_duration = 0
    total_distance = 0

    for i in range(len(req.points)-1):
        o = req.points[i]
        d = req.points[i+1]
        params = {
            "key": AMAP_KEY,
            "origin": o,
            "destination": d,
            "show_fields": "cost",  # 只取耗时（cost.duration）；距离是基础字段
        }
        params.update(extra)

        r = requests.get(base_url, params=params, timeout=25)
        logging.info("AMap request(segment): %s", r.url)
        data = r.json()
        if not data or data.get("status") != "1":
            return {"status": "0", "info": data.get("info") if isinstance(data, dict) else "AMap error", "debug": {"url": r.url, "text": r.text[:200]}}

        try:
            path = data["route"]["paths"][0]
            distance = int(path.get("distance", 0)) if path.get("distance") is not None else 0
            dur = 0
            if path.get("cost") and path["cost"].get("duration"):
                dur = int(float(path["cost"]["duration"]))
        except Exception:
            distance, dur = 0, 0

        total_distance += distance
        total_duration += dur
        segments.append({
            "from": o,
            "to": d,
            "distance": distance,
            "duration": dur
        })

    return {
        "status": "1",
        "mode": req.mode,
        "total_distance": total_distance,
        "total_duration": total_duration,
        "segments": segments
    }
