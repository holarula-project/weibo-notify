from asyncio import run
from datetime import datetime
from json import dumps, loads
from os import environ
from os.path import basename, exists
from sqlite3 import Row, connect
from time import sleep

from aiohttp import ClientSession
from discord import Embed, File, Object, Webhook, WebhookMessage

con = connect("./weibo-crawler/weibo/weibodata.db")
con.row_factory = Row
cur = con.cursor()

WEBHOOK_URL = environ.get("WEBHOOK_URL")
webhook: Webhook = None


# 发送记录状态
STATUS = {
    "UNSENT": 0,  # 没有发送记录，新微博
    "SENT": 1,  # 已经发送过一次了
    "RESENT": 2,  # 二次发送，更新留言
}


def check_sent(weibo: Row) -> int:
    """检查发送记录

    Args:
        weibo (Row): 微博数据

    Returns:
        int: 发送记录状态
    """
    file = open("./sent.json", "r+", encoding="utf-8")
    data = file.read()
    if len(data) == 0:
        data = "[]"
        file.write(data)
    file.close()
    sent = loads(data)
    for item in sent:
        if item["id"] == weibo["id"] or item["bid"] == weibo["bid"]:
            return item["status"]
    return STATUS["UNSENT"]


def should_resent(weibo: Row):
    """检查第一次发送是否是24小时前，如果是的话就二次更新

    Args:
        weibo (Row): 微博数据

    Returns:
        bool: 是否应该二次更新
    """
    file = open("./sent.json", "r+", encoding="utf-8")
    data = file.read()
    file.close()
    sent = loads(data)
    for item in sent:
        if item["id"] == weibo["id"] or item["bid"] == weibo["bid"]:
            return (
                datetime.now() - datetime.fromisoformat(item["updated_at"])
            ).days >= 1
    return False


def update_sent(weibo: Row, msg_id: int, status: int):
    """更新发送状态

    Args:
        weibo (Row): 微博数据
        msg_id (int): Discord 信息 ID
        status (int): 发送状态
    """

    send_obj = {
        "id": weibo["id"],
        "bid": weibo["bid"],
        "msg_id": msg_id,
        "status": status,
        "updated_at": datetime.now().isoformat(),
    }

    # 获取现记录
    file = open("./sent.json", "r", encoding="utf-8")
    data = file.read()
    file.close()

    file = open("./sent.json", "w", encoding="utf-8")

    if len(data) == 0:  # 空白文件
        file.write(dumps([send_obj]))
    else:
        sent = loads(data)
        if len(sent) == 0:  # 空数组
            file.write(dumps([send_obj], ensure_ascii=False, indent=2))
        else:
            # 查询发送记录
            found = -1
            for idx, item in enumerate(sent):
                if item["id"] == weibo["id"] or item["bid"] == weibo["bid"]:
                    found = idx
            if found == -1:
                sent.append(send_obj)
            else:
                sent[found] = send_obj
            file.write(dumps(sent, ensure_ascii=False, indent=2))
    file.close()


def get_msg_id(weibo: Row):
    """获取已发送微博的 Discord 信息 ID

    Args:
        weibo (Row): 微博数据

    Raises:
        Exception: 未必发送此微博到Discord，查无记录

    Returns:
        int: Discord 信息 ID
    """
    file = open("./sent.json", "r", encoding="utf-8")
    data = file.read()
    sent = loads(data)
    file.close()
    for idx, item in enumerate(sent):
        if item["id"] == weibo["id"] or item["bid"] == weibo["bid"]:
            return item["msg_id"]

    raise Exception("未发送过信息。")


async def get_msg_by_id(id: int):
    """获取 Discord 信息 ID 对象

    Args:
        id (int): Discord 信息 ID

    Returns:
        WebhookMessage: webhook 发送过的 Discord 信息
    """
    return await webhook.fetch_message(id, thread=Object(id))


async def create_thread(weibo: Row):
    """发送微博文字信息到discord

    Args:
        weibo (dict): 微博数据
    """

    thread: WebhookMessage = None

    # 每次最多4096字符
    for i in range(0, len(weibo["text"]), 4096):
        msg = Embed(
            title=weibo["id"],
            description=weibo["text"][i : i + 4096],
            timestamp=datetime.strptime(weibo["created_at"], "%Y-%m-%d %H:%M:%S"),
            color=4886754,
            type="rich",
            url=f"https://weibo.com/{weibo['user_id']}/{weibo['bid']}",
        )

        if thread is None:
            msg.set_footer(text=weibo["source"])
            msg.add_field(name="转发", value=weibo["reposts_count"], inline=True)
            msg.add_field(name="留言", value=weibo["comments_count"], inline=True)
            msg.add_field(name="点赞", value=weibo["attitudes_count"], inline=True)

            thread_name = f"微博@{weibo['created_at']}"

            if len(weibo["text"].strip()) >= 100:
                thread_name = weibo["text"][0:99] + "…"
            elif len(weibo["text"].strip()) > 0:
                thread_name = weibo["text"][0:100]

            thread = await webhook.send(embed=msg, wait=True, thread_name=thread_name)
        else:
            await webhook.send(embed=msg, thread=thread)

    return thread


async def resent_updated_msg(weibo: Row, thread: WebhookMessage):
    """二次发送时检查微博是否更新过

    Args:
        weibo (Row): 微博数据
        thread (WebhookMessage): 回复的目标 Discord 微博帖子
    """
    for idx, embed in enumerate(thread.embeds):
        discord_text = embed.description.strip()
        weibo_text = weibo["text"][idx * 4096 : 4096].strip()
        if discord_text != weibo_text:
            msg = Embed(
                title=weibo["id"],
                description=weibo_text,
                timestamp=datetime.strptime(weibo["created_at"], "%Y-%m-%d %H:%M:%S"),
                color=4886754,
                type="rich",
                url=f"https://weibo.com/{weibo['user_id']}/{weibo['bid']}",
            )
            if idx == 0:
                msg.set_footer(text=weibo["source"])
                msg.add_field(name="转发", value=weibo["reposts_count"], inline=True)
                msg.add_field(name="留言", value=weibo["comments_count"], inline=True)
                msg.add_field(name="点赞", value=weibo["attitudes_count"], inline=True)
            await webhook.send(embed=msg, wait=True, thread=thread)


async def send_pics(weibo: Row, thread: WebhookMessage):
    """回复微博附带图片到 Discord 的微博帖子

    Args:
        weibo (Row): 微博数据
        thread (WebhookMessage): 回复的目标 Discord 微博帖子
    """
    if len(weibo["pics"]) > 0:
        img_files = []
        pics = cur.execute(
            "SELECT * FROM bins WHERE weibo_id = ?", (weibo["id"],)
        ).fetchall()

        urls = weibo["pics"].split(",")

        for url in urls:
            for pic in pics:
                if url == pic["url"] and exists(pic["path"]):
                    img_files.append(File(pic["path"], basename(pic["path"])))
                    break

        # 每次最多发送9张图片
        for i in range(0, len(img_files), 9):
            files = img_files[i : i + 9]
            if len(files) > 0:
                await webhook.send(files=files, thread=thread)


async def send_vid(weibo: Row, thread: WebhookMessage):
    """回复微博附带视频到 Discord 的微博帖子

    Args:
        weibo (Row): 微博数据
        thread (WebhookMessage): 回复的目标 Discord 微博帖子
    """
    if len(weibo["video_url"]) > 0:
        vids = cur.execute(
            "SELECT * FROM bins WHERE weibo_id = ?", (weibo["id"],)
        ).fetchall()

        for vid in vids:
            if weibo["video_url"] == vid["url"] and exists(vid["path"]):
                await webhook.send(
                    files=[File(vid["path"], basename(vid["path"]))], thread=thread
                )


async def send_comments(weibo: Row, thread: WebhookMessage):
    """回复微博留言到 Discord 的微博帖子

    Args:
        weibo (Row): 微博数据
        thread (WebhookMessage): 回复的目标 Discord 微博帖子
    """
    comments = cur.execute(
        "SELECT * FROM comments WHERE weibo_id = ?", (weibo["id"],)
    ).fetchall()
    comment_list = [""]
    cidx = 0

    for comment in comments:
        new_comment = f"> `{'🦝 ' if comment['user_id'] == '1789152110' else '💬 '}{comment['user_screen_name']} 📅({comment['created_at']})`:\n```\n{comment['text']}\n```\n"
        # 每个信息最多2000字符
        if len(comment_list[cidx]) + len(new_comment) > 1990:
            comment_list.append(new_comment)
            cidx += 1
        comment_list[cidx] += new_comment

    # 每次2000字符多次发送
    for comment_msg in comment_list:
        if len(comment_msg.strip()) > 0:
            await webhook.send(comment_msg, thread=thread)


async def update_divider(thread):
    await webhook.send(
        "```\n" + ("-" * 25) + datetime.now().isoformat() + ("-" * 25) + "\n```",
        thread=thread,
    )


async def main():
    async with ClientSession() as session:
        global webhook
        webhook = Webhook.from_url(WEBHOOK_URL, session=session)

        weibos = cur.execute("SELECT * FROM weibo ORDER BY created_at ASC").fetchall()
        for idx, weibo in enumerate(weibos):
            print(f"{idx+1}/{len(weibos)}/{weibo['bid']}")
            sent_status = check_sent(weibo)
            if sent_status == STATUS["UNSENT"]:
                thread = await create_thread(weibo)
                sleep(2)
                await send_pics(weibo, thread)
                sleep(2)
                await send_vid(weibo, thread)
                sleep(2)
                await send_comments(weibo, thread)
                sleep(2)
                update_sent(weibo, thread.id, STATUS["RESENT"])
            elif sent_status == STATUS["SENT"] and should_resent(weibo):
                msg_id = get_msg_id(weibo)
                thread = await get_msg_by_id(msg_id)
                sleep(2)
                await update_divider(thread)
                sleep(2)
                await resent_updated_msg(weibo, thread)
                sleep(2)
                await send_comments(weibo, thread)
                sleep(2)
                update_sent(weibo, msg_id, STATUS["RESENT"])


if __name__ == "__main__":
    run(main())
