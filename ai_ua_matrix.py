#!/usr/bin/env python3
import argparse, hashlib, json, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PROFILES = [
  ("normal", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/127 Safari/537.36"),
  ("chatgpt-user", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
  ("claude-user", "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)"),
  ("oai-searchbot", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36; compatible; OAI-SearchBot/1.4; +https://openai.com/searchbot"),
  ("bytespider-experimental", "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)"),
]
BLOCK = ("verify you are human", "prove your humanity", "checking your browser", "access denied", "captcha", "blocked by network security")

def one(url, profile, ua, timeout):
    t=time.time(); raw=b""; status=None; final=url; ctype=""; err=None
    try:
        req=Request(url,headers={"User-Agent":ua,"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9"})
        with urlopen(req, timeout=timeout) as r:
            status=r.status; final=r.geturl(); ctype=r.headers.get("content-type",""); raw=r.read(2_000_000)
    except HTTPError as e:
        status=e.code; final=e.geturl(); ctype=e.headers.get("content-type","") if e.headers else ""
        try: raw=e.read(2_000_000)
        except Exception: raw=b""
        err=repr(e)
    except Exception as e: err=repr(e)
    text=raw.decode("utf-8","replace")
    low=text.lower()
    return {"profile":profile,"status":status,"final_url":final,"content_type":ctype,"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"blocked_marker":any(x in low for x in BLOCK),"elapsed_seconds":round(time.time()-t,3),"error":err,"body_head":text[:1000]}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("job"); ap.add_argument("--output",default="matrix.json"); a=ap.parse_args()
    j=json.loads(Path(a.job).read_text()); timeout=int(j.get("timeout_seconds",25)); rows=[]
    for url in j["urls"]:
        rows.append({"url":url,"profiles":[one(url,p,u,timeout) for p,u in PROFILES]})
    Path(a.output).write_text(json.dumps({"job":j.get("name"),"rows":rows},ensure_ascii=False,indent=2)+"\n")
if __name__=="__main__": main()
