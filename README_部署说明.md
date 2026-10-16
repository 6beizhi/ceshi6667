# 宠安星 PETSAFE STAR — 可部署网站及运营后台

这是一个 **Python 3.9+ 标准库 + SQLite + HTML/CSS/JavaScript** 的实际可运行小型网站。HTML 页面由 `app.py` 服务端动态生成，静态 CSS/JS 放在 `static/`；`bin/` 为本机启动脚本。无需 Node、npm、Flask 等依赖。数据是服务器端数据库，不再是浏览器 localStorage。

## 已提供

- 用户注册、登录、退出、密码 PBKDF2 加盐哈希；数据库会话、CSRF 防护、基本权限检查。
- 宠物档案、家庭寄养/上门喂养/遛宠预约；服务价格及 30% 押金**计算与展示**；订单查看。
- 主人交接确认、管理员交接记录/服务方确认、小信封阅读确认；照护时间戳记录；三级异常上报与管理员人工复核。
- 管理员工作台：订单查询与状态操作、宠托师审核与暂停接单、异常审核。
- 移动端和电脑端响应式页面，页脚 `© 2026 XBAOS`。
- 管理约束：小信封和双方交接未确认不能进入交接后订单状态；无打卡记录不能结单。

## 本机运行（Windows / macOS / Linux）

1. 安装 Python 3.9 或更新版本，解压 ZIP 到一个文件夹。
2. **设置初始管理员（仅首次初始化数据库时创建）：**
   - Windows PowerShell:
     ```powershell
     $env:ADMIN_EMAIL="admin@example.com"
     $env:ADMIN_PASSWORD="替换为至少12位的复杂密码"
     python app.py
     ```
   - Linux/macOS:
     ```bash
     export ADMIN_EMAIL='admin@example.com'
     export ADMIN_PASSWORD='替换为至少12位的复杂密码'
     python3 app.py
     ```
3. 浏览器访问 `http://127.0.0.1:8000`。管理员登录使用上述邮箱和密码，普通用户可自行注册。
4. 第二次启动无需再次提供管理员环境变量，数据保存在 `data/petsafe.sqlite3`。**不要将此数据库公开上传到网站根目录。**

> `.env.example` 仅是说明样例。Python 标准库不会自动加载 `.env` 文件，请通过环境变量、systemd 的 `EnvironmentFile` 或服务器管理面板配置。

## 服务器部署（Ubuntu + Nginx + systemd，示例）

将整个目录上传至服务器，例如 `/opt/petsafe/`，确保专用服务账户可写入数据库目录。安装 Python 3.9+、Nginx。先使用管理员变量运行一次完成建库，并确认目录权限。生产建议为服务创建独立 Linux 用户和持久化数据目录 `/var/lib/petsafe`，而非使用 root 运行。

**systemd:** `/etc/systemd/system/petsafe.service` 示例：

```ini
[Unit]
Description=PetSafe Star
After=network.target
[Service]
User=petsafe
Group=petsafe
WorkingDirectory=/opt/petsafe
Environment=HOST=127.0.0.1
Environment=PORT=8000
Environment=COOKIE_SECURE=1
Environment=PETSAFE_DATA_DIR=/var/lib/petsafe
ExecStart=/usr/bin/python3 /opt/petsafe/app.py
Restart=always
RestartSec=3
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
```

然后执行 `sudo systemctl daemon-reload && sudo systemctl enable --now petsafe`。

**Nginx:** `/etc/nginx/sites-available/petsafe` 示例：

```nginx
server {
    listen 80;
    server_name your-domain.example;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

配置域名 DNS 后，通过 Certbot 等方法签发 HTTPS 证书，并启用 HTTP→HTTPS 跳转；启用 HTTPS 后保持 `COOKIE_SECURE=1`。Nginx 配置检测：`sudo nginx -t && sudo systemctl reload nginx`。访问 `https://你的域名/health` 应返回 `ok`。

## 部署边界与重要提醒

- **这是可实际运行、可部署的小规模试运行系统，不代表可直接开展真实商业交易。** 不含微信/支付宝收款、押金扣退、线上签名法律效力、视频摄像头接入、媒体上传/存证、AI识别、保险承保、短信、真实客服通知、自动理赔，也不声称已经实现这些功能。
- `bin/` 是启动脚本，不是支付接口或后端的二进制程序。后台由 `app.py` 提供。
- 单进程 `wsgiref` 适合验证和低并发测试，公网正式运营建议使用 Gunicorn 等成熟 WSGI Server（`pip install gunicorn && gunicorn --bind 127.0.0.1:8000 --workers 2 app:application`），反向代理、系统防火墙、限速、防暴力登录、备份、监控和日志需要部署人员完善。
- 网站使用 SQLite 持久化数据库；定期备份数据库并限制其文件权限。正式运营还应完成等保/隐私合规、服务者实名审核、协议及取消押金规则、支付和风险机制。
- 本项目的示例价格为服务试运行示意值，不是最终对客合同报价。
- **静态网站平台（Cloudflare Pages 等）不能单独执行 Python/SQLite 后端。** 请使用有 Python 进程支持的 VPS、云服务器、容器服务等部署本项目。
