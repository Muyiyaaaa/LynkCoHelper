---
# 郑重告知：本程序源码仅供学习研究使用，使用该程序造成的一切后果与程序作者无关。本项目并非拿来即用，有技术门槛！！！
---

支持领克 App 每日签到、分享任务、积分查询与 Bark 推送，可通过 GitHub Actions 定时运行。仓库同时提供独立的图文动态评论任务，默认演练，发布需显式开启。

## 功能状态

### 已完成

- [x] 每日签到、积分（能量体）查询与前后对比
- [x] 分享任务（简化两步法 + 完整三步法回退），随机抽取探索广场最新文章分享，避免固定文章下架失效
- [x] Bark 推送每日任务结果（含分享文章标题展示）
- [x] 独立图文动态评论任务：从 `/home-page/v3/list` 读取文章，通过 ChatAnywhere/GLM 生成评论，支持文章发布、Bark 逐条通知和 GitHub Actions 状态去重
- [x] token 自动续期（refreshToken 双方案兜底）+ 本地过期时间缓存，避免调试时频繁续期
- [x] 短信验证码登录全流程（含本地极验滑块辅助页面）
- [x] GitHub Actions 定时执行，环境变量 / `env.json` 双配置方式，密钥零硬编码
- [x] 多账号支持（`env.json` 的 `users` 数组）：配置 ≥2 个账号后每日任务自动执行双账号互点分享（互为访客走完整三步法，每号每天额外一次"被点击"加分机会），并计入 Bark 推送
- [x] 签到里程碑（连续 7/30/85/365 天）进度解析与推送展示；`lynkco_sign.py --milestones` / `--repair YYYY-MM-DD` 子命令
- [x] 探索广场点赞（真机抓包确认端点 `/app/explore/home-page/article/operation/agree`）：`lynkco_daily_tasks.py --like` 一键点赞最新文章
- [x] 积分任务清单查询（真机抓包确认端点 `/up/api/v1/user/collection/detail/point`，官方 22 项任务）：`lynkco_daily_tasks.py --tasks` 打印全部任务的奖励与完成条件，推送中展示任务统计
- [x] 抓包凭据一键回写：`tools/apply_capture_credentials.py` 从 mitmproxy 抓包（`lynkco_capture.py` 落盘的 JSONL）提取最新 token/refreshToken/deviceId 写回 `env.json`，refreshToken 过期后免手动滑块登录

### 暂未完成 / 已知限制

- [ ] 账号密码登录（`login_by_password`）：仅为参考实现，密码加密算法未完全逆向确认，且陌生设备会被风控拦截，不建议依赖
- [ ] 分享任务仅支持"文章"类型内容，"动态"类型的正确分享链接格式尚未验证
- [ ] 评论任务目前只自动发布已验证的文章类型；普通 UGC 图文可读取和演练，但不会进入定时发布
- [ ] 登录滑块验证码全自动化：极验人机验证无法自动完成，仍需人工操作一次
- [ ] 里程碑奖励领取 / 补签的写操作端点尚未抓包确认（本次抓包未覆盖：当天无达标里程碑、无缺签日）：已预留 `env.json["extraEndpoints"]`（或 `LYNKCO_EP_REWARD_RECEIVE` / `LYNKCO_EP_SIGN_REPAIR` 环境变量）注入通道，抓包确认路径填入即启用，未配置时安全跳过、不影响主任务

详细说明（配置方式、token 获取、AppSecret 自动提取、图文评论任务）见 [`LynkCoHelper/readme.md`](LynkCoHelper/readme.md)。
