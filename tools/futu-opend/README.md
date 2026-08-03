# Futu OpenD（Linux 无界面）

当前安装的是富途官方 `10.9.6918` Ubuntu x86_64 版本。

## 首次登录

```bash
cd /home/or1ngelinux/Liberty/tools/futu-opend/headless
./first-login.sh <牛牛号>
```

脚本会在终端中隐藏读取密码。密码不会写入 Shell 历史或进程参数；包含密码
MD5 的短期配置文件权限为 `600`，OpenD 读取后会自动删除。MD5 只是富途
OpenD 要求的传输格式，并不等同于安全加密。

如果 Linux 设备首次登录触发设备锁，在 OpenD 控制台依次输入：

```text
req_phone_verify_code
input_phone_verify_code -code=收到的六位短信验证码
```

## 后续启动

首次登录成功并记住设备后：

```bash
cd /home/or1ngelinux/Liberty/tools/futu-opend/headless
./start.sh
```

OpenD 行情 API 仅监听 `127.0.0.1:11111`，本机运维端口仅监听
`127.0.0.1:22222`。当前启动参数禁用模拟交易，只用于行情数据。

## Python SDK 与只读检查

SDK 安装在项目独立环境 `tools/futu-opend/.venv`，版本锁定见
`requirements.txt`。保持 OpenD 运行后执行：

```bash
/home/or1ngelinux/Liberty/tools/futu-opend/.venv/bin/python \
  /home/or1ngelinux/Liberty/scripts/support/check_futu_opend.py
```

检查脚本只请求港股、A 股和美股快照，不请求历史 K 线，因此不会占用
100 个历史 K 线标的额度。

## 后台服务

用户级 systemd 单元源文件位于 `systemd/futu-opend.service`。启用后可用：

```bash
systemctl --user status futu-opend.service
journalctl --user -u futu-opend.service
systemctl --user restart futu-opend.service
systemctl --user stop futu-opend.service
```

当前 Linux 用户已启用 linger，因此服务可在退出 SSH 后继续运行，并在系统
启动后由用户级 systemd 拉起。
