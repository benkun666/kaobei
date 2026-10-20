import os, time, hmac, base64, hashlib, random, string
from urllib.parse import quote
import requests
from flask import Flask, jsonify, request, Response
from io import BytesIO
from PIL import Image
from urllib.parse import quote, urlparse
import ipaddress, socket

app = Flask(__name__)
DEFAULT_API = "api.copy2000.online"
SECRET = base64.b64decode("M2FmMDg1OTAzMTEwMzJlZmUwNjYwNTUwYTA1NjNhNTM=")
UMSTRING = "b4c89ca4104ea9a97750314d791520ac"
session = requests.Session()
comic_cache = {}

def headers():
    ts = str(int(time.time()))
    sig = hmac.new(SECRET, ts.encode(), hashlib.sha256).hexdigest()
    digits = string.digits
    device = random.choice(string.ascii_uppercase) + random.choice(string.ascii_uppercase) + random.choice(digits) + random.choice(string.ascii_uppercase) + "." + "".join(random.choice(digits) for _ in range(6)) + "." + "".join(random.choice(digits) for _ in range(3))
    return {"User-Agent":"COPY/3.0.6","source":"copyApp","deviceinfo":f"{random.randint(1000000,9999999)}V-{random.randint(1000,9999)}","dt":time.strftime("%Y.%m.%d"),"platform":"3","referer":"com.copymanga.app-3.0.6","version":"3.0.6","device":device,"pseudoid":"".join(random.choice(string.ascii_letters+digits) for _ in range(16)),"Accept":"application/json","region":"0","authorization":"Token","umstring":UMSTRING,"x-auth-timestamp":ts,"x-auth-signature":sig}

def api_base():
    try:
        r = session.get("https://api.copy-manga.com/api/v3/system/network2?platform=3", headers=headers(), timeout=12)
        if r.ok:
            host = r.json().get("results",{}).get("api",[[None]])[0][0]
            if host:
                return "https://" + host.replace("https://","").replace("http://","").strip("/")
    except Exception:
        pass
    return "https://" + DEFAULT_API

def upstream(path):
    r = session.get(api_base()+path, headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def cover_url(url):
    if not url: return ""
    return "https:"+url if url.startswith("//") else url

def safe_public_image_url(url):
    """Allow only public HTTP(S) image hosts; block local/private network targets."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname
        if host.lower() in ("localhost",):
            return False
        # Reject literal private/local IPs and hostnames resolving to them.
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)]
            except Exception:
                return False
        return bool(addresses) and all(ip.is_global for ip in addresses)
    except Exception:
        return False

@app.get("/image.jpg")
def image_jpg():
    """Convert an upstream comic page to real JPEG bytes for JPG-only watches."""
    url = request.args.get("url", "")
    if not safe_public_image_url(url):
        return jsonify({"code":400,"message":"图片地址无效"}),400
    try:
        r = session.get(url, headers={"User-Agent":"Mozilla/5.0","Accept":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"}, timeout=25, stream=True)
        r.raise_for_status()
        raw = r.content
        if not raw:
            return jsonify({"code":502,"message":"上游图片为空"}),502
        with Image.open(BytesIO(raw)) as source:
            source.load()
            # JPEG cannot represent alpha; flatten transparent pages onto white.
            if source.mode in ("RGBA", "LA") or (source.mode == "P" and "transparency" in source.info):
                rgba = source.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255,255,255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                converted = background
            else:
                converted = source.convert("RGB")
            output = BytesIO()
            converted.save(output, format="JPEG", quality=92, optimize=True)
        return Response(output.getvalue(), mimetype="image/jpeg", headers={"Cache-Control":"public, max-age=86400"})
    except Exception:
        app.logger.exception("Failed to fetch or convert comic page")
        return jsonify({"code":502,"message":"图片下载或JPG转换失败"}),502

def comic_data(cid):
    cid = str(cid)
    if cid in comic_cache: return comic_cache[cid]
    data = upstream(f"/api/v3/comic2/{quote(cid)}?in_mainland=true&request_id=&platform=3").get("results",{})
    comic = data.get("comic",{})
    chapters = []
    for group in (data.get("groups") or {}).values():
        path = group.get("path_word")
        if not path: continue
        listing = upstream(f"/api/v3/comic/{quote(cid)}/group/{quote(str(path))}/chapters?limit=100&offset=0&in_mainland=true&request_id=")
        results = listing.get("results",{})
        chapters.extend(results.get("list") or [])
        total = int(results.get("total") or 0)
        offset = 100
        while offset < min(total, 500):
            more = upstream(f"/api/v3/comic/{quote(cid)}/group/{quote(str(path))}/chapters?limit=100&offset={offset}&in_mainland=true&request_id=")
            chapters.extend(more.get("results",{}).get("list") or [])
            offset += 100
    comic_cache[cid] = {"comic":comic,"chapters":chapters}
    return comic_cache[cid]

@app.get("/")
def home():
    return jsonify({"name":"拷贝漫画腕上漫画源","status":"running","config":"/config"})

@app.get("/health")
def health():
    return jsonify({"ok":True})

@app.get("/config")
def config():
    base = request.host_url.rstrip("/")
    return jsonify({"CopyManga":{"name":"拷贝漫画","apiUrl":base,"detailPath":"/album/<id>","photoPath":"/photo/<id>/chapter/<chapter>","searchPath":"/search/<text>/<page>","type":"copymanga"}})

@app.get("/search/<path:keyword>/<int:page>")
def search(keyword,page):
    try:
        offset=max(0,page-1)*30
        data=upstream(f"/api/v3/search/comic?limit=30&offset={offset}&q={quote(keyword,safe='')}&q_type=").get("results",{})
        rows=data.get("list") or []
        out=[]
        for row in rows:
            c=row.get("comic") if isinstance(row.get("comic"),dict) else row
            cid=c.get("path_word") or c.get("id")
            if cid:
                out.append({"comic_id":str(cid),"title":c.get("name") or "未命名漫画","cover_url":cover_url(c.get("cover")),"pages":0})
        total=int(data.get("total") or len(out))
        return jsonify({"page":page,"has_more":offset+len(out)<total,"results":out})
    except Exception:
        return jsonify({"code":502,"message":"上游搜索暂时不可用，请稍后重试"}),502

@app.get("/album/<cid>")
def album(cid):
    try:
        packed=comic_data(cid); c=packed["comic"]
        return jsonify({"item_id":str(cid),"name":c.get("name") or "未命名漫画","page_count":1,"views":c.get("popular",0),"rate":0,"cover":cover_url(c.get("cover")),"tags":[x.get("name") for x in (c.get("theme") or []) if isinstance(x,dict) and x.get("name")],"total_chapters":max(1,len(packed["chapters"]))})
    except Exception:
        return jsonify({"code":502,"message":"漫画详情暂时不可用"}),502

@app.get("/photo/<cid>/chapter/<int:n>")
def photo(cid,n):
    try:
        packed=comic_data(cid); chapters=packed["chapters"]
        if n<1 or n>len(chapters): return jsonify({"code":404,"message":"章节不存在"}),404
        ch=chapters[n-1]; chapter_id=ch.get("uuid")
        data=upstream(f"/api/v3/comic/{quote(str(cid))}/chapter2/{quote(str(chapter_id))}?in_mainland=true&request_id=").get("results",{}).get("chapter",{})
        contents=data.get("contents") or []; words=data.get("words") or []
        urls=[None]*len(contents)
        for i,item in enumerate(contents):
            url=item.get("url") if isinstance(item,dict) else None
            order=words[i] if i<len(words) and isinstance(words[i],int) else i
            if url:
                url=cover_url(url)
                if 0<=order<len(urls): urls[order]=url
                else: urls[i]=url
        base = request.host_url.rstrip("/")
        return jsonify({"title":ch.get("name") or f"第{n}话","images":[{"url":base+"/image.jpg?url="+quote(u, safe="")} for u in urls if u]})
    except Exception:
        return jsonify({"code":502,"message":"章节图片暂时不可用"}),502

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","3000")))
