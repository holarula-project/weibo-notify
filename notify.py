import subprocess
from asyncio import run
from datetime import datetime
from json import dumps, loads
from os import environ, stat
from os.path import abspath, basename, exists
from shlex import split
from sqlite3 import Row, connect
from sys import argv
from time import sleep

import boto3
from aiohttp import ClientSession
from discord import Embed, File, Object, Webhook, WebhookMessage

con = connect("./weibo-crawler/weibo/weibodata.db")
con.row_factory = Row
cur = con.cursor()

WEBHOOK_URL = environ.get("WEBHOOK_URL")
webhook: Webhook = None

sent_list = ""

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
    file = open(sent_list, "r+", encoding="utf-8")
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
    file = open(sent_list, "r+", encoding="utf-8")
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
    file = open(sent_list, "r", encoding="utf-8")
    data = file.read()
    file.close()

    file = open(sent_list, "w", encoding="utf-8")

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


def get_file_mb(file: File):
    """获取文件大小

    Args:
        file (File): 文件

    Returns:
        float: 文件 MB 大小
    """
    return stat(file.fp.name).st_size / (1024 * 1024.0)


def get_msg_id(weibo: Row):
    """获取已发送微博的 Discord 信息 ID

    Args:
        weibo (Row): 微博数据

    Raises:
        Exception: 未必发送此微博到Discord，查无记录

    Returns:
        int: Discord 信息 ID
    """
    file = open(sent_list, "r", encoding="utf-8")
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

        start = 0
        end = 0
        payload_size = 0
        i = 0
        while i < len(img_files):
            mb = get_file_mb(img_files[i])

            if mb > 25:
                raise Exception("单文件不可大于25MB")

            # 加新文件少于 25 MB 以及总文件数少于 9
            if payload_size + mb < 25 and (end - start) < 9:
                end += 1
                payload_size += mb
                i += 1

                if i == len(img_files):
                    await webhook.send(files=img_files[start:end], thread=thread)
            # 每次最多发送9张图片 or 25 MB
            elif payload_size + mb > 25 or (end - start) == 9:
                await webhook.send(files=img_files[start:end], thread=thread)
                start = i
                end = i
                payload_size = 0


def upload_to_s3(file: File):
    """上传视频到兼容S3的服务器。

    Args:
        file (File): 视频文件对象
    """
    s3 = boto3.client(
        service_name="s3",
        endpoint_url=environ.get("S3_URL"),
        aws_access_key_id=environ.get("S3_KEY_ID"),
        aws_secret_access_key=environ.get("S3_KEY_SECRET"),
        region_name=environ.get("S3_REGION"),
    )

    s3.upload_file(file.fp.name, environ.get("S3_BUCKET"), file.filename)
    s3.close()


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
                file = File(vid["path"], basename(vid["path"]))
                mb = get_file_mb(file)
                err_msg = subprocess.run(
                    split(f"ffmpeg -v error -i {abspath(vid['path'])} -f null -"),
                    capture_output=True,
                ).stderr.decode("utf-8")
                if mb > 25:
                    upload_to_s3(file)
                    msg = f"> 已上传视频: `{file.filename}` ({round(mb, 2)}MB)"
                    if len(err_msg) > 0:
                        msg += f"\n${err_msg}"
                    await webhook.send(msg)
                else:
                    await webhook.send(
                        content=err_msg if len(err_msg) > 0 else "",
                        files=[file],
                        thread=thread,
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
        emoji = "💬"

        if comment["user_id"] == "1789152110":
            emoji = "🦝"
        elif comment["user_id"] == "2297117134":
            emoji = "🧵"
        new_comment = f"> `{emoji} {comment['user_screen_name']} 📅({comment['created_at']})`:\n```\n{comment['text']}\n```\n"
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


def check():
    if len(argv) != 2:
        raise Exception("必须传入一个参数")

    if argv[1] not in ("holarula", "senjoukun"):
        raise Exception(f'无效参数 "{argv[1]}"')

    global sent_list
    sent_list = f"./{argv[1]}_sent.json"


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
                update_sent(weibo, thread.id, STATUS["SENT"])
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
    check()
    run(main())
