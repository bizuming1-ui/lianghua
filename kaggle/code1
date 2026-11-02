# ==========================================================================
#  Cell 0 · 在 Kaggle「GPU T4 x2」上部署推理大模型(vLLM 张量并行 TP=2,两张卡同时计算)
#  使用前提:Settings → Accelerator = GPU T4 x2;Internet = ON
#  默认模型:Qwen/Qwen3-14B-AWQ(稠密 4bit、带"思考模式"的推理模型,T4 上可用且速度可接受)
#  想换模型:运行前设置环境变量 QUANT_LLM_MODEL,例如 Qwen/Qwen3-32B-AWQ(更强但更慢)
#  运行顺序:先运行本单元(首次约 8~20 分钟:安装 vLLM + 下载模型 + 加载),再运行 Cell 1
#  本单元结尾会做"双卡同时工作"实测:并发请求时同时采样两张卡的利用率与显存
# ==========================================================================
import os, sys, re, time, json, subprocess, threading, urllib.request

MODEL = os.environ.get("QUANT_LLM_MODEL", "Qwen/Qwen3-14B-AWQ")
SERVED = os.environ.get("QUANT_LLM_NAME", "quant-llm")
PORT = int(os.environ.get("QUANT_LLM_PORT", "8001"))
MAX_LEN = int(os.environ.get("QUANT_LLM_CTX", "8192"))
ROOT = os.environ.get("QUANT_ROOT", "/kaggle/working/quant")
os.makedirs(ROOT, exist_ok=True)
LOG = os.path.join(ROOT, "vllm.log")
BASE = "http://127.0.0.1:%d" % PORT


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def gpus():
    r = sh("nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits")
    out = []
    for line in r.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 5:
            out.append((int(p[0]), p[1], float(p[2]), float(p[3]), float(p[4])))
    return out


def up():
    try:
        urllib.request.urlopen(BASE + "/v1/models", timeout=2).read()
        return True
    except Exception:
        return False


# 1) 检查两张 GPU ------------------------------------------------------------
g = gpus()
print("检测到 GPU:", [(x[0], x[1], "%dMiB" % x[4]) for x in g])
if len(g) < 2:
    raise SystemExit("✘ 只检测到 %d 张 GPU。请在右侧 Settings → Accelerator 选择『GPU T4 x2』后重启 Notebook。" % len(g))
print("✔ 双 GPU 就绪:", ", ".join("GPU%d %s" % (x[0], x[1]) for x in g))

if up():
    print("✔ 大模型服务已在运行(%s),跳过安装与启动。" % BASE)
else:
    # 2) 安装 vLLM --------------------------------------------------------------
    have = sh("%s -c 'import vllm,sys;print(vllm.__version__)'" % sys.executable)
    if have.returncode != 0:
        print("… 安装 vLLM(约 3~8 分钟,请耐心等待)")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "vllm", "huggingface_hub"])
        if r.returncode != 0:
            raise SystemExit("✘ vLLM 安装失败,请把上面的报错发给我")
        have = sh("%s -c 'import vllm;print(vllm.__version__)'" % sys.executable)
    print("✔ vLLM 版本:", have.stdout.strip())

    # 3) 启动(失败自动降级重试:eager 模式 → 更小上下文)-----------------------------
    attempts = [
        ["--max-model-len", str(MAX_LEN), "--gpu-memory-utilization", "0.90"],
        ["--max-model-len", str(MAX_LEN), "--gpu-memory-utilization", "0.88", "--enforce-eager"],
        ["--max-model-len", "4096", "--gpu-memory-utilization", "0.85", "--enforce-eager"],
    ]
    ok = False
    for n, extra in enumerate(attempts, 1):
        sh("pkill -f 'vllm.entrypoints' ; sleep 3")
        cmd = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", MODEL, "--served-model-name", SERVED,
               "--tensor-parallel-size", "2", "--dtype", "half", "--host", "127.0.0.1", "--port", str(PORT),
               "--max-num-seqs", "8"] + extra
        env = dict(os.environ, PYTHONUNBUFFERED="1", TOKENIZERS_PARALLELISM="false", VLLM_WORKER_MULTIPROC_METHOD="spawn")
        print("… 第 %d 次启动:%s" % (n, " ".join(cmd[3:])))
        proc = subprocess.Popen(cmd, env=env, stdout=open(LOG, "w"), stderr=subprocess.STDOUT, start_new_session=True)
        t0 = time.time()
        last = 0
        while time.time() - t0 < 1500:
            if up():
                ok = True
                break
            if proc.poll() is not None:
                break
            if time.time() - last > 30:
                last = time.time()
                tail = ""
                try:
                    tail = open(LOG, errors="ignore").read().strip().splitlines()[-1][:110]
                except Exception:
                    pass
                mem = " | ".join("GPU%d %d/%dMiB" % (x[0], x[3], x[4]) for x in gpus())
                print("  [%3ds] %s  ||  %s" % (time.time() - t0, mem, tail))
            time.sleep(3)
        if ok:
            break
        print("✘ 本次启动失败,日志末尾:")
        print("\n".join(open(LOG, errors="ignore").read().splitlines()[-25:]))
    if not ok:
        raise SystemExit("✘ vLLM 三次启动均失败。请把上面的日志发给我。")
    print("✔ 大模型服务已启动:", BASE)

# 4) 双卡同时工作实测 ------------------------------------------------------------
print("\n… 双卡并发实测(4 路并发生成,同时采样两张卡)")
results = []


def ask(i):
    body = {"model": SERVED, "max_tokens": 200, "temperature": 0.7,
            "messages": [{"role": "user", "content": "用中文详细解释第 %d 种降低投资组合风险的方法。" % (i + 1)}],
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    t = time.time()
    j = json.load(urllib.request.urlopen(req, timeout=300))
    results.append((j["usage"]["completion_tokens"], time.time() - t))


ths = [threading.Thread(target=ask, args=(i,)) for i in range(4)]
t0 = time.time()
for t in ths:
    t.start()
peak = {}
while any(t.is_alive() for t in ths):
    for x in gpus():
        p = peak.setdefault(x[0], [0.0, 0.0, x[4]])
        p[0] = max(p[0], x[2])
        p[1] = max(p[1], x[3])
    time.sleep(0.4)
for t in ths:
    t.join()
el = time.time() - t0
tot = sum(r[0] for r in results)
print("生成 %d tokens / %.1fs = %.1f tok/s(4 路并发总吞吐)" % (tot, el, tot / max(el, 1e-6)))
both = True
for i in sorted(peak):
    util, mem, total = peak[i]
    print("  GPU%d  峰值利用率 %3d%%   显存 %5d / %d MiB" % (i, util, mem, total))
    both = both and util >= 10 and mem >= 2000
print("✅ 两张 T4 同时工作(张量并行 TP=2)" if both else "⚠ 有一张卡利用率偏低,请把上面输出发给我排查")
print("\n下一步:运行 Cell 1(启动量化终端 + 公网网址),在页面右侧『AI 推理决策』面板点『AI』模式即可。")
