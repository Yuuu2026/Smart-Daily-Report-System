"""日报发送的固定配置。

改这个文件就等于改命令行参数：不用打开网页，也不用问 AI。
改完直接运行，就按这里的设置发出去：

    py -3.12 scripts/report_cli.py

命令行参数仍然优先于本文件，用于临时改一次：
    py -3.12 scripts/report_cli.py --to someone@example.com

只想看效果不发信：
    py -3.12 scripts/report_cli.py --dry-run
"""

# 发布者账号，对应数据库 publisher_users.username。
# 留空字符串时，自动取「上一次发布日报的人」。
USER = "songhaoyu"

# 收件邮箱，可以写多个，例如 ["a@x.com", "b@y.com"]。
# 留空列表 [] 时，回落到数据库里这个账号配置过的通知邮箱。
RECIPIENTS = ["3247026746@qq.com"]

# 发件服务器。四项全部留空时，沿用 .smtp.local.json 里已经存好的配置（推荐）。
# 只有想完全在代码里接管发信时才填。
# 注意：SMTP_PASSWORD 是明文，本仓库是公开的，填了等于把授权码公开，
# 确认要这么做再填，或者改用 .smtp.local.json 那条路。
SMTP_HOST = ""
SMTP_PORT = ""
SMTP_USERNAME = ""
SMTP_PASSWORD = ""
SMTP_SECURE = ""

# 今天投入的工作时长（小时），写成 "6.5" 这样的字符串。
# 留空则表示不统计，邮件概览里不显示这一格。
HOURS = ""

# 邮件详细程度，只能是这三个值之一：简洁 / 标准 / 详细
REPORT_DETAIL = "标准"

# 是否附带当天已同步到数据库里的飞书群聊消息。
INCLUDE_FEISHU = True

# 发送前是否先调一次飞书接口拉取群聊（接口只能拉到最近 5 分钟的消息）。
SYNC_FEISHU = False

# True  = 不询问，打印完草稿直接发送，适合挂定时任务或提交钩子。
# False = 打印草稿后逐项询问收件人、是否带飞书、是否确认。
AUTO_SEND = False
