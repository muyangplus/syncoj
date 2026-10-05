# 部署与安装

## 一键安装（三种入口，共用同一份逻辑）

```bash
# 1. 镜像预装（构建考试机镜像时跑，网络可达）
sudo python3 install.py \
    --download-url https://10.0.0.1:8443/dist/syncoj-agent-bundle.tar.gz \
    --sha256 <从服务端界面抄下来的校验和> \
    --server https://10.0.0.1:8443 \
    --enroll-code XXXX-XXXX-XXXX-XXXX

# 2. 离线包安装（考场无网，U 盘拷过去）
sudo python3 install.py \
    --bundle ./syncoj-agent-0.1.0.tar.gz \
    --sha256 <校验和> \
    --server https://10.0.0.1:8443 \
    --enroll-code XXXX-XXXX-XXXX-XXXX

# 3. 在线自举（一条命令）
curl -fsSL https://10.0.0.1:8443/dist/bootstrap.sh | sudo sh -s -- \
    --server https://10.0.0.1:8443 --enroll-code XXXX-XXXX-XXXX-XXXX

# 先看看会做什么（不需要 root，不做任何改动）
python3 install.py --bundle ./x.tar.gz --server https://x --dry-run
```

## 安装后的布局

```
/opt/syncoj/
├── releases/
│   ├── 0.1.0/syncoj_agent/...   每个版本一份，互不覆盖
│   └── 0.1.1/syncoj_agent/...
└── current -> releases/0.1.1    原子切换的软链；systemd 跑的就是它

/etc/syncoj/agent.ini            配置（已存在时**不覆盖**，见下）
/var/lib/syncoj/                 凭据、哈希缓存、日志、未完成的下载
```

版本化目录 + `current` 软链的布局是自更新的前提。若不用安装器而手工部署成
单一目录，`upgrade.mode = apply` 会直接失败。

## 幂等性

安装器可以随便重复执行，结果一致。三条关键保证：

1. **绝不覆盖已存在的 `agent.ini`**。教师很可能已经改过扫描目录或注册码，
   覆盖是灾难性的。要重建请显式加 `--force-config`。
2. **同版本不重复解压**。已装过的版本直接复用。
3. **已是当前版本则不重启服务**。避免每次跑安装器都打断正在进行的传输。

## 打包

```bash
# 产出 dist/syncoj-agent-<版本>.tar.gz
python3 build_bundle.py

# 顺便用服务端私钥签一份 .sig（供手工核对）
python3 build_bundle.py --verify-signature /etc/syncoj/release-key.pem
```

打包是**可复现的**：同样的内容产出逐字节相同的压缩包，与输出文件名无关。
gzip 头里默认嵌的时间戳与文件名、tar 成员里的 mtime 都已被压平 —— 否则
"同一版本两次打包校验和不同"，签名的确定性和"这个包是不是那个包"的判断都会失效。

## 卸载

```bash
sudo systemctl disable --now syncoj-agent
sudo rm -f /etc/systemd/system/syncoj-agent.service
sudo systemctl daemon-reload
sudo rm -rf /opt/syncoj /etc/syncoj
# 状态目录里是凭据与日志，确认不需要留档后再删
sudo rm -rf /var/lib/syncoj
sudo userdel syncoj
```

## 排障

```bash
systemctl status syncoj-agent          # 进程是否在跑
journalctl -u syncoj-agent -n 100      # systemd 侧的启动期输出
tail -f /var/lib/syncoj/agent.log      # Agent 自己的日志（自动轮转）
/usr/bin/python3 -E -s /opt/syncoj/current/syncoj_agent/main.py \
    --config /etc/syncoj/agent.ini --check      # 只校验配置
/usr/bin/python3 -E -s /opt/syncoj/current/syncoj_agent/main.py \
    --config /etc/syncoj/agent.ini --once       # 只跑一轮，前台看输出
```

> 注意 `-E -s`：忽略所有 `PYTHON*` 环境变量与 user site-packages。选手怎么
> `pip install` 都污染不到 Agent。手工调试时也请带上，否则看到的不是真实行为。
