# ==========================================================================
#  生物科技ETF 量化终端 + AI 推理决策 · Kaggle 一键启动(Cell 1)
#  建议顺序:先运行 Cell 0(双 T4 部署大模型),再运行本单元;没有 Cell 0 时仍可用规则策略
#  使用前提:Notebook 右侧 Settings → Internet 必须是 ON(行情和公网网址都需要联网)
#  运行后:下方会打印一个 https://xxxx.trycloudflare.com 网址,点开就是量化终端
#  说明:服务以后台进程运行,本单元运行完即返回;会话不关闭,网站就一直在线
# ==========================================================================
import os, sys, re, time, subprocess, urllib.request

# ★ 数据永久保存(强烈建议配置,否则 Kaggle 重开后账户/成交/AI 复盘会丢):
#   到 https://github.com/settings/tokens 新建 classic token,只勾选 gist 权限,填在下面;
#   或在 Kaggle Notebook 菜单 Add-ons → Secrets 添加名为 GITHUB_TOKEN 的密钥(推荐,不会泄露到代码里)。
#   系统会自动在你的 GitHub 建一个【私有 Gist】保存备份,下次启动自动找回。
GITHUB_TOKEN = ""

ROOT = os.environ.get("QUANT_ROOT", "/kaggle/working/quant")
PORT = int(os.environ.get("QUANT_PORT", "8000"))
os.makedirs(ROOT, exist_ok=True)

SERVER_PY = r'''__SERVER_PY__'''

AI_PY = r'''__AI_PY__'''

MATH_PY = r'''__MATH_PY__'''

INDEX_HTML = r'''__INDEX_HTML__'''

INDICATORS_PY = r'''__INDICATORS_PY__'''

REFLECT_PY = r'''__REFLECT_PY__'''

PERSIST_PY = r'''__PERSIST_PY__'''

for _name, _body in (("server.py", SERVER_PY), ("ai_engine.py", AI_PY), ("quant_math.py", MATH_PY), ("index.html", INDEX_HTML),
                     ("indicators.py", INDICATORS_PY), ("reflect.py", REFLECT_PY), ("persist.py", PERSIST_PY)):
    with open(os.path.join(ROOT, _name), "w", encoding="utf-8") as f:
        f.write(_body)
try:
    import numpy  # AI 数学内核依赖(Kaggle 自带)
except Exception:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "numpy"])
print("✔ 文件已写入", ROOT)

# 1) 清理旧进程(方括号写法避免 pkill 误杀自身所在的 shell)
subprocess.run("pkill -f '[q]uant/server.py'; pkill -f '[c]loudflared tunnel'; pkill -f '[l]ocalhost.run'", shell=True)
time.sleep(1)

# 2) 启动后端(行情轮询 + 模拟交易 + 网页)
_tok = (GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN") or "").strip()
if not _tok:
    try:
        from kaggle_secrets import UserSecretsClient
        _tok = UserSecretsClient().get_secret("GITHUB_TOKEN").strip()
    except Exception:
        _tok = ""
env = dict(os.environ, PORT=str(PORT), PYTHONUNBUFFERED="1", QUANT_GITHUB_TOKEN=_tok)
print("☁ 云备份:", "已配置 GitHub Token(将自动备份/恢复到私有 Gist)" if _tok else "⚠ 未配置 GITHUB_TOKEN → 数据只在本次会话有效!(见本单元顶部说明)")
server_log = open(os.path.join(ROOT, "server.log"), "w")
server = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, cwd=ROOT,
                          stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
ok = False
for _ in range(60):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT, timeout=1).read()
        ok = True
        break
    except Exception:
        if server.poll() is not None:
            break
        time.sleep(0.5)
if not ok:
    print(open(os.path.join(ROOT, "server.log")).read())
    raise SystemExit("✘ 后端启动失败,请把上面的日志发给我")
print("✔ 后端已启动: http://127.0.0.1:%d" % PORT)
try:
    import json as _json
    _h = _json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT, timeout=2))
    print("✔ AI 数学内核已加载" if _h.get("ai") else "⚠ AI 模块未加载(缺少 numpy?),规则策略仍可用")
    _p = _json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/persist" % PORT, timeout=2))
    print("♻ 数据恢复:", (_p.get("restored") or {}).get("detail") or "未找到历史备份(全新开始)", "· 来源:", (_p.get("restored") or {}).get("from") or "-")
    if (_p.get("cloud") or {}).get("err"):
        print("⚠ 云备份读取问题:", _p["cloud"]["err"])
    try:
        urllib.request.urlopen("http://127.0.0.1:8001/v1/models", timeout=2).read()
        print("✔ 检测到大模型服务(Cell 0 已就绪),页面里可直接选『AI』模式")
    except Exception:
        print("ℹ 尚未检测到大模型服务:请先运行 Cell 0(双 T4 部署),再刷新页面使用『AI』模式")
except Exception:
    pass

# 联网自检:新浪行情源
try:
    req = urllib.request.Request("https://hq.sinajs.cn/list=sz159502", headers={"Referer": "https://finance.sina.com.cn"})
    urllib.request.urlopen(req, timeout=4).read()
    print("✔ 行情源可访问(新浪)")
except Exception as ex:
    print("⚠ 行情源不可访问:%s  → 请确认 Settings → Internet 已开启;否则页面会显示『模拟行情』" % ex)

# 3) 公网隧道:优先 cloudflared(免账号),失败则回退 localhost.run(ssh)
def wait_url(path, pattern, tries=60):
    for _ in range(tries):
        time.sleep(1)
        try:
            m = re.search(pattern, open(path).read())
            if m:
                return m.group(0)
        except Exception:
            pass
    return None

public_url = None
try:
    cf = os.path.join(ROOT, "cloudflared")
    if not os.path.exists(cf):
        print("… 下载 cloudflared")
        urllib.request.urlretrieve(
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64", cf)
        os.chmod(cf, 0o755)
    cf_log = os.path.join(ROOT, "cloudflared.log")
    subprocess.Popen([cf, "tunnel", "--url", "http://127.0.0.1:%d" % PORT, "--no-autoupdate"],
                     stdout=open(cf_log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    public_url = wait_url(cf_log, r"https://[a-z0-9-]+\.trycloudflare\.com")
except Exception as ex:
    print("⚠ cloudflared 不可用:", ex)

if not public_url:
    print("… 改用 localhost.run 隧道")
    lh_log = os.path.join(ROOT, "lhr.log")
    subprocess.Popen(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
                      "-R", "80:127.0.0.1:%d" % PORT, "nokey@localhost.run"],
                     stdout=open(lh_log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    public_url = wait_url(lh_log, r"https://[a-z0-9]+\.lhr\.life")

if public_url:
    with open(os.path.join(ROOT, "url.txt"), "w") as f:
        f.write(public_url)
    print("\n" + "=" * 60)
    print("🚀 量化终端网址(点击打开):", public_url)
    print("=" * 60)
    print("提示:刚生成的网址偶尔需要 10~20 秒才生效,打不开就稍等刷新。")
    try:
        from IPython.display import HTML, display
        display(HTML('<a href="%s" target="_blank" style="font-size:20px">👉 打开量化终端</a>' % public_url))
    except Exception:
        pass
else:
    print("✘ 公网隧道建立失败。后端仍在 127.0.0.1:%d 运行;请确认 Internet 已开启后重新运行本单元。" % PORT)
