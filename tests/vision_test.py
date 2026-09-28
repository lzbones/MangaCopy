#!/usr/bin/env python3
"""Vision capability test for spark via litellm proxy. Writes only to /tmp."""
import base64, json, subprocess, sys, time, urllib.request, uuid

API = "http://166.111.50.17:4000/v1/chat/completions"
KEY = "sk-qingxu-litellm-thicv639"
SRC = "/Users/qingxu/Documents/Software/AI/MangaCopy/Ref/第187话/0001.png"
SMALL = "/tmp/manga_1024.png"

QUESTION = "这页漫画里有几个人物、大致是什么场景？请简要回答。"

def ask_vision(img_path, model="spark"):
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": QUESTION},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 500,
        "metadata": {"session_id": str(uuid.uuid4())},
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read().decode()
            dt = time.time() - t0
            data = json.loads(body)
            content = data["choices"][0]["message"].get("content", "")
            print(f"[{model}] {img_path} -> HTTP {resp.status}, {dt:.2f}s")
            print(f"CONTENT: {content[:600]}")
            return True
    except urllib.error.HTTPError as e:
        dt = time.time() - t0
        err = e.read().decode()[:500]
        print(f"[{model}] {img_path} -> HTTP {e.code}, {dt:.2f}s")
        print(f"ERROR: {err}")
    except Exception as e:
        print(f"[{model}] {img_path} -> EXC: {e}")
    return False

if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "spark"
    img = sys.argv[2] if len(sys.argv) > 2 else SRC
    ok = ask_vision(img, model)
    sys.exit(0 if ok else 1)
