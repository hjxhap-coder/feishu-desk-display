#!/usr/bin/env python3
"""
三屏渲染器 v3 —— 按参考图的风格重做。

参考图给了四条明确指令：
  1. 整块内容有**黑色外边框**（像一块摆件，而不是一张截图）
  2. 页眉是**大字标题 + 右侧时间 + 一条粗横线**
  3. 日程屏用**竖直时间轴**（圆点 + 连线），进行中的整块反白 + 空心圈
  4. 底部一条**横向时间轴**：两端时间标签，轴上一排小圆点，中间一根粗竖线标"现在"

v3 相比 v2 的取舍：
  - 待办屏从"按到期分组"改回**平铺 + 排序**（逾期排最前），因为参考图就是平铺，
    而分组标签在 1-bit 上太重，跟外边框、页眉抢注意力。
  - 日程屏底部的横轴从"时长黑条"改成**小圆点**（照参考图），点的位置 = 事件时刻。
  - 待机屏改成**左书法 / 右时钟**。

排版全在这台 Mac 上做完（含中文），ESP32 只负责把字节贴到屏上。
"""
import argparse
import json
import os
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "v3")
STATE = os.path.join(HERE, "state_v3.json")
ASSETS = os.path.join(HERE, "assets")
W, H = 400, 300
S = 4                                  # 先在 4 倍尺寸上画，再缩回 1-bit，边缘更干净

BLACK, WHITE = 0, 255

# 字体系统：1-bit 屏上细笔画会被阈值化打散，所以小字一律用 Medium 兜底。
# title = 宋体黑，笔画有粗细变化，比黑体多一分碑刻味，最接近参考图的页眉。
FONT_FACES = [
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0, "title"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2, "w6"),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1, "med"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, "w3"),
    ("/Library/Fonts/Arial Unicode.ttf", 0, "sans"),
]
_cache = {}

WEEK = "一二三四五六日"
MODES = ("standby", "todo", "schedule")
MIN_PX = 11                                            # 小于这个字号在 1-bit 上不可读

# 日程屏底部方案：axis = 横向时间轴带（默认），gap = 空档 + 倒计时
RIBBON_MODE = os.environ.get("TODO_RIBBON", "axis")

# 版面基准
FRAME = 5 * S          # 外边框离画布边缘
M = 12 * S             # 内容左右边距
INK = None             # 《快雪时晴帖》素材，懒加载


def ink_asset():
    """左栏的书法局部。1-bit 素材，1=纸白 0=墨黑。"""
    global INK
    if INK is None:
        p = os.path.join(ASSETS, "kuaixue_ink.png")
        INK = Image.open(p).convert("1") if os.path.exists(p) else None
    return INK


def face(weight):
    """按字重取字体，缓存住。"""
    if weight in _cache:
        return _cache[weight]
    for path, idx, name in FONT_FACES:
        if name == weight and os.path.exists(path):
            try:
                _cache[weight] = (path, idx)
                return _cache[weight]
            except Exception:
                continue
    for path, idx, name in FONT_FACES:
        if os.path.exists(path):
            _cache[weight] = (path, idx)
            return _cache[weight]
    _cache[weight] = (None, 0)
    return _cache[weight]


def font(size, weight="w3"):
    """取字体。小字号自动提到 W6 —— 1-bit 屏上细笔画会被阈值化打散。"""
    px = size // S
    if px < 14 and weight in ("w3", "med"):
        weight = "w6"
    path, idx = face(weight)
    if path is None:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size, index=idx)
    except Exception:
        return ImageFont.load_default()


def text_w(d, s, f):
    return d.textbbox((0, 0), s, font=f)[2]


def track(d, xy, s, f, fill, spacing):
    """带字距的文字，返回总宽。用于小号标签，制造印刷感。"""
    x, y = xy
    for ch in s:
        d.text((x, y), ch, font=f, fill=fill)
        x += d.textbbox((0, 0), ch, font=f)[2] + spacing
    return x - xy[0] - spacing


def wrap(d, s, f, maxw):
    """逐字贪心换行，中英文都适用。"""
    lines, cur = [], ""
    for ch in s:
        if text_w(d, cur + ch, f) <= maxw:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def new_canvas():
    img = Image.new("L", (W * S, H * S), WHITE)
    return img, ImageDraw.Draw(img)


def finish(img):
    """缩回 400x300 并二值化。1 = 白（纸面），0 = 黑（墨）。"""
    img = img.resize((W, H), Image.LANCZOS)
    return img.point(lambda v: 0 if v < 140 else 255, mode="1")


def pack(bw):
    """1-bit 位图 -> 15000 字节，行优先，MSB 在前，1 = 白。"""
    px = bw.load()
    data = bytearray()
    for y in range(H):
        for xb in range(W // 8):
            b = 0
            for i in range(8):
                if px[xb * 8 + i, y]:
                    b |= 1 << (7 - i)
            data.append(b)
    return bytes(data)


# ---------------------------------------------------------------------------
# 通用零件
# ---------------------------------------------------------------------------
def rule(d, x0, y, x1, weight=1, fill=BLACK):
    d.line([(x0, y), (x1, y)], fill=fill, width=weight * S)


def frame(d):
    """外边框。屏幕要像一块摆件，不像一张截图。"""
    d.rectangle([FRAME, FRAME, W * S - FRAME - 1, H * S - FRAME - 1],
                outline=BLACK, width=S)


def seg_bar(d, x, y, w, h, done, total, gap=2 * S, invert=False):
    """分段进度条：一格一个任务，实心=已完成。

    invert=True 用于黑底上：已完成画白块，未完成画黑块配白框。
    """
    total = max(total, 1)
    seg = (w - gap * (total - 1)) / total
    for i in range(total):
        x0 = x + i * (seg + gap)
        if invert:
            if i < done:
                d.rectangle([x0, y, x0 + seg, y + h],
                            fill=WHITE, outline=WHITE, width=S)
            else:
                d.rectangle([x0, y, x0 + seg, y + h],
                            fill=BLACK, outline=WHITE, width=S)
        else:
            d.rectangle([x0, y, x0 + seg, y + h],
                        fill=BLACK if i < done else WHITE,
                        outline=BLACK, width=S)


def checkbox(d, x, y, size, checked):
    """复选框：实心方块 + 一道白勾 / 空心方框。"""
    if checked:
        d.rectangle([x, y, x + size, y + size], fill=BLACK)
        d.line([(x + size * 0.22, y + size * 0.52),
                (x + size * 0.42, y + size * 0.74)], fill=WHITE, width=max(2 * S, 1))
        d.line([(x + size * 0.42, y + size * 0.74),
                (x + size * 0.80, y + size * 0.26)], fill=WHITE, width=max(2 * S, 1))
    else:
        d.rectangle([x, y, x + size, y + size], outline=BLACK, width=S)


def label(d, xy, s, size=MIN_PX, fill=BLACK, spacing=None):
    """小号字距标签（印刷体的栏目名）。"""
    size = max(size, MIN_PX)
    f = font(size * S, "med")
    sp = spacing if spacing is not None else int(size * S * 0.2)
    track(d, xy, s, f, fill, sp)


def header(d, title, now, M):
    """页头：宋体大字 + 右侧时间 + 一条粗横线。返回下一行 y。"""
    f = font(23 * S, "title")
    d.text((M, M - 3 * S), title, font=f, fill=BLACK)
    t = now.strftime("%H:%M")
    ft = font(16 * S, "med")
    d.text((W * S - M - text_w(d, t, ft), M + 3 * S), t, font=ft, fill=BLACK)
    y = M + 27 * S
    rule(d, M, y, W * S - M, 3)
    return y + 9 * S


def footer(d, left, right, M):
    """页脚：细分隔线 + 左右两段小字。"""
    fy = H * S - 21 * S
    rule(d, M, fy - 4 * S, W * S - M, 1)
    fs = font(MIN_PX * S, "med")
    d.text((M, fy), left, font=fs, fill=BLACK)
    d.text((W * S - M - text_w(d, right, fs), fy), right, font=fs, fill=BLACK)


def notice_card(d, M, top, width, title, detail):
    """未授权 / 异常提示：整块反白。

    1-bit 屏上「没有待办」和「待办没权限」长得一模一样（都是空列表）。
    所以这一路失败时必须显式画出来。
    """
    f_t = font(16 * S, "w6")
    f_d = font(MIN_PX * S, "med")
    h = 56 * S
    d.rectangle([M, top, M + width, top + h], fill=BLACK)
    d.text((M + 12 * S, top + 8 * S), title, font=f_t, fill=WHITE)
    d.text((M + 12 * S, top + 32 * S), detail, font=f_d, fill=WHITE)


def err_note(state, key, what):
    """(state, key) -> (标题, 副标题) 或 None。

    只在这一路**确实没数据**时才提示。ok_tasks=True 但数据是 mock 补的
    —— 那是"演示数据"，不是"未授权"，两者在屏上必须区分开。
    """
    if state.get("ok_%s" % key) is not False:
        return None
    errors = state.get("errors") or {}
    err = errors.get(key)
    if not err:
        return "%s不可用" % what, "数据源不可用"
    if err.startswith("缺权限"):
        return "%s未授权" % what, "缺权限 " + err[len("缺权限"):].strip()
    return "%s不可用" % what, err[:30]


def mock_tag(state, key):
    """这一路是不是 mock 顶的。"""
    mock = state.get("mock") or {}
    return bool(mock.get(key))


# ---------------------------------------------------------------------------
# 待机：左书法 / 右时间
# ---------------------------------------------------------------------------
def _countdown(target_ts, now):
    """距离某个时刻还有多久，写成一句人话。"""
    if not target_ts:
        return ""
    delta = target_ts - now.timestamp()
    if delta <= 0:
        return "正在进行"
    mins = int(delta // 60)
    if mins < 60:
        return "还有 %d 分钟" % max(mins, 1)
    h, m = divmod(mins, 60)
    if h < 24:
        return "还有 %d 小时 %d 分" % (h, m)
    return "还有 %d 天 %d 小时" % (h // 24, h % 24)


def render_standby(state, now=None):
    now = now or datetime.now()
    img, d = new_canvas()
    frame(d)

    SPLIT = 150 * S          # 左右分栏
    RIGHT_X = SPLIT + 12 * S
    RIGHT_W = W * S - M - RIGHT_X

    # ---- 左栏：快雪时晴帖局部 ----
    ink = ink_asset()
    if ink is not None:
        # 素材是 1-bit 的，按 4 倍贴回画布，最终缩回 400x300 正好还原成原像素
        big = ink.resize((ink.width * S, ink.height * S), Image.NEAREST)
        img.paste(big, (M, 14 * S))
        ih = ink.height * S            # 画布尺度下的高度
    else:
        ih = 0

    # 左栏底部：日期
    f_d = font(12 * S, "med")
    ds = "%d月%d日 星期%s" % (now.month, now.day, WEEK[now.weekday()])
    dy = 14 * S + ih + 10 * S
    d.text((M, dy), ds, font=f_d, fill=BLACK)

    # 分栏竖线
    d.line([(SPLIT, 14 * S), (SPLIT, H * S - 34 * S)], fill=BLACK, width=S)

    # ---- 右栏：时间 ----
    f_clock = font(64 * S, "w6")
    t = now.strftime("%H:%M")
    d.text((RIGHT_X + (RIGHT_W - text_w(d, t, f_clock)) // 2, 38 * S),
           t, font=f_clock, fill=BLACK)

    f_sm = font(MIN_PX * S, "med")
    rule(d, RIGHT_X, 116 * S, W * S - M, 1)

    # 下一个日程
    y = 126 * S
    label(d, (RIGHT_X, y), "下一个日程", MIN_PX, BLACK, 2 * S)
    y += 15 * S
    nxt = state.get("next_event")
    if nxt:
        fn = font(15 * S, "w6")
        when = nxt.get("start_hm", "")
        wt = 0
        if when:
            d.text((RIGHT_X, y), when, font=fn, fill=BLACK)
            wt = text_w(d, when, fn)
        d.text((RIGHT_X + wt + 7 * S, y), nxt.get("title", "")[:12], font=fn, fill=BLACK)
        cd = _countdown(nxt.get("start_ts") or nxt.get("start"), now)
        if cd:
            d.text((RIGHT_X, y + 20 * S), cd, font=f_sm, fill=BLACK)
    else:
        d.text((RIGHT_X, y), "今天没有日程", font=font(13 * S, "w3"), fill=BLACK)

    # 今日概览：反白块
    y = 186 * S
    total = state.get("total_tasks", 0)
    done = total - state.get("open_tasks", 0)
    n_ev = len(state.get("events", []))
    d.rectangle([RIGHT_X, y, W * S - M, y + 54 * S], fill=BLACK)
    d.text((RIGHT_X + 10 * S, y + 8 * S), "今日 %d 场会" % n_ev,
           font=font(14 * S, "w6"), fill=WHITE)
    if state.get("ok_tasks") is False:
        d.text((RIGHT_X + 10 * S, y + 29 * S), "待办未授权", font=f_sm, fill=WHITE)
    else:
        tag = " 演示" if mock_tag(state, "tasks") else ""
        d.text((RIGHT_X + 10 * S, y + 29 * S),
               "待办 %d / %d 已完成%s" % (done, total, tag), font=f_sm, fill=WHITE)
        seg_bar(d, RIGHT_X + 10 * S, y + 43 * S, RIGHT_W - 20 * S, 5 * S,
                done, min(total, 20) or 1, invert=True)

    # 底部反白条：按键说明（放在外框内侧）
    band_y = H * S - FRAME - 24 * S
    d.rectangle([FRAME + 1, band_y, W * S - FRAME - 1, H * S - FRAME - 1], fill=BLACK)
    fb = font(MIN_PX * S, "med")
    hints = ["待机", "待办", "日程"]
    cw = (W * S - 2 * FRAME) // 3
    for i, h in enumerate(hints):
        cx = FRAME + cw * i + 18 * S
        d.rectangle([cx, band_y + 8 * S, cx + 7 * S, band_y + 15 * S],
                    outline=WHITE, width=S)
        d.text((cx + 11 * S, band_y + 7 * S), h, font=fb, fill=WHITE)
    return finish(img)


# ---------------------------------------------------------------------------
# 待办
# ---------------------------------------------------------------------------
def _due_rank(it, today_md):
    """排序权重：逾期 0 / 今天 1 / 明天 2 / 以后 3 / 无期限 4。"""
    due = (it.get("due") or "").strip()
    if not due:
        return 4
    if due.startswith("昨天"):
        return 0
    if due.startswith("今天"):
        return 1
    if due.startswith("明天"):
        return 2
    if len(due) >= 5 and due[2] == "-":
        return 0 if due[:5] < today_md else 3
    return 3


def render_todo(state, now=None):
    now = now or datetime.now()
    img, d = new_canvas()
    frame(d)

    y = header(d, "待办", now, M)

    note = err_note(state, "tasks", "待办")
    if note:
        notice_card(d, M, y + 30 * S, W * S - 2 * M, note[0], note[1])
        footer(d, "待办清单", "未授权", M)
        return finish(img)

    items = state.get("tasks", [])
    done = sum(1 for i in items if i.get("done"))
    total = len(items)

    # 进度条：方块串（照参考图），右边跟计数
    seg_bar(d, M, y, W * S - 2 * M - 52 * S, 8 * S, done, min(total, 20) or 1)
    fc = font(13 * S, "w6")
    ct = "%d/%d" % (done, total)
    d.text((W * S - M - text_w(d, ct, fc), y - 2 * S), ct, font=fc, fill=BLACK)
    y += 22 * S

    # v3：平铺 + 排序。参考图就是平铺，逾期排最前补回分组丢掉的信息。
    today_md = now.strftime("%m-%d")
    ordered = sorted(items, key=lambda it: (_due_rank(it, today_md), it.get("due") or ""))

    f_item = font(15 * S, "w3")
    f_item_b = font(15 * S, "w6")
    f_small = font(MIN_PX * S, "med")
    row_h = 24 * S
    maxw = W * S - 2 * M - 62 * S
    footer_y = H * S - 25 * S

    for it in ordered:
        if y + row_h > footer_y:
            d.text((M, y), "还有 %d 项" % (len(ordered) - ordered.index(it)),
                   font=f_small, fill=BLACK)
            break
        checked = bool(it.get("done"))
        bs = 13 * S
        checkbox(d, M, y + 2 * S, bs, checked)

        tx = M + bs + 9 * S
        if it.get("pri") == "high" and not checked:
            d.rectangle([tx, y + 6 * S, tx + 5 * S, y + 11 * S], fill=BLACK)
            tx += 11 * S

        lines = wrap(d, it.get("text", ""), f_item, maxw)
        for i, ln in enumerate(lines):
            f = f_item_b if (not checked and i == 0) else f_item
            d.text((tx, y + i * 18 * S), ln, font=f, fill=BLACK)
            if checked and i == 0:
                wln = text_w(d, ln, f)
                mid = y + 9 * S
                d.line([(tx, mid), (tx + wln, mid)], fill=BLACK, width=S)

        if it.get("due"):
            due = it["due"]
            d.text((W * S - M - text_w(d, due, f_small), y + 4 * S), due,
                   font=f_small, fill=BLACK)
        y += row_h

    upd = state.get("updated_at", "")
    left = "待办清单"
    if mock_tag(state, "tasks"):
        left += " · 演示数据"
    footer(d, left, (upd[-5:] + " 更新") if upd else "", M)
    return finish(img)


# ---------------------------------------------------------------------------
# 日程
# ---------------------------------------------------------------------------
def _span(ev):
    st = ev.get("start_ts", ev.get("start"))
    en = ev.get("end_ts", ev.get("end"))
    if st is None:
        return None
    if en is None:
        en = st + 1800
    if en < st:
        en = st + 600
    return st, en


def _remaining(ev, now):
    """进行中的事件还剩多久。"""
    en = ev.get("end_ts") or ev.get("end")
    if not en:
        return ""
    mins = int((en - now.timestamp()) // 60)
    if mins <= 0:
        return ""
    if mins < 60:
        return "剩余 %d 分" % mins
    h, m = divmod(mins, 60)
    return "剩余 %d 小时 %d 分" % (h, m)


def _spans(events):
    out = []
    for ev in events:
        sp = _span(ev)
        if sp:
            out.append((sp[0], sp[1], ev))
    out.sort()
    return out


def ribbon_axis(d, events, now, M, top):
    """方案 A：横向时间轴带。

    事件按真实时刻落成小圆点，「现在」是一根贯穿的粗竖线。
    优点：一屏看尽全天的疏密和位置关系，是「日历」而不是「文字」。
    缺点：占 28px 高度，且需要眯眼找圆点。
    """
    spans = _spans(events)
    if not spans:
        return
    # 固定成办公时间 08:00 - 20:00，比按事件自适应更可预期
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = (day + timedelta(hours=8)).timestamp()
    t1 = (day + timedelta(hours=20)).timestamp()
    nf = now.timestamp()
    if nf < t0:
        t0 = day.timestamp()
    elif nf > t1:
        t1 = (day + timedelta(hours=23, minutes=59)).timestamp()
    span = t1 - t0

    ax_y = top + 22 * S
    x0, x1 = M, W * S - M

    def x_of(t):
        return x0 + (t - t0) / span * (x1 - x0)

    f_lab = font(MIN_PX * S, "med")
    l0 = datetime.fromtimestamp(t0).strftime("%H:%M")
    l1 = datetime.fromtimestamp(t1).strftime("%H:%M")
    d.text((x0, top), l0, font=f_lab, fill=BLACK)
    d.text((x1 - text_w(d, l1, f_lab), top), l1, font=f_lab, fill=BLACK)

    d.line([(x0, ax_y), (x1, ax_y)], fill=BLACK, width=S)

    for st, en, ev in spans:
        px = x_of(st)
        r = 3 * S
        if ev.get("next"):
            d.ellipse([px - r, ax_y - r, px + r, ax_y + r], outline=BLACK, width=S)
        else:
            d.ellipse([px - r, ax_y - r, px + r, ax_y + r], fill=BLACK)

    nx = x_of(now.timestamp())
    d.line([(nx, ax_y - 9 * S), (nx, ax_y + 7 * S)], fill=BLACK, width=2 * S)


def ribbon_gap(d, events, now, M, top):
    """方案 B：空档提示 + 下一场倒计时。

    用文字回答两个问题：「我什么时候有空」「下一场是什么、还有多久」。
    优点：直接给结论，零解读成本，比圆点好懂。
    缺点：丢掉全天的疏密感，且一天会议多时空档本身就不可信。
    """
    rule(d, M, top - 6 * S, W * S - M, 1)
    f_lab = font(MIN_PX * S, "med")
    f_val = font(13 * S, "w6")

    spans = _spans(events)
    now_ts = now.timestamp()

    # 今天接下来最大的空档（>= 45 分钟）
    best = None
    if spans:
        cur = now_ts
        for st, en, _ in spans:
            if en <= now_ts:
                cur = max(cur, en)
                continue
            st = max(st, cur)
            if st - cur >= 45 * 60:
                best = (cur, st)
                break
            cur = max(cur, en)

    nxt = next((ev for _, _, ev in spans if ev.get("next")), None)
    if nxt is None:
        nxt = next((ev for _, _, ev in spans if ev[0] > now_ts), None)

    y = top + 2 * S
    if best:
        a = datetime.fromtimestamp(best[0])
        b = datetime.fromtimestamp(best[1])
        mins = int((best[1] - best[0]) // 60)
        h, m = divmod(mins, 60)
        span_txt = "%d 小时 %d 分" % (h, m) if h else "%d 分" % m
        d.text((M, y), "空闲 %s–%s（%s）" % (a.strftime("%H:%M"),
                                             b.strftime("%H:%M"), span_txt),
               font=f_lab, fill=BLACK)
    else:
        d.text((M, y), "接下来没有空档", font=f_lab, fill=BLACK)
    y += 15 * S

    if nxt:
        when = nxt.get("start_hm", "")
        cd = _countdown(nxt.get("start_ts"), now)
        d.text((M, y), "下一场 %s %s" % (when, nxt.get("title", "")[:11]),
               font=f_val, fill=BLACK)
        if cd:
            d.text((W * S - M - text_w(d, cd, f_lab), y + 3 * S), cd,
                   font=f_lab, fill=BLACK)
    else:
        d.text((M, y), "今天没有更多日程", font=f_val, fill=BLACK)


def render_schedule(state, now=None):
    now = now or datetime.now()
    img, d = new_canvas()
    frame(d)

    y = header(d, "日程", now, M)

    note = err_note(state, "events", "日程")
    if note:
        notice_card(d, M, y + 30 * S, W * S - 2 * M, note[0], note[1])
        footer(d, "今日日程", "未授权", M)
        return finish(img)

    events = state.get("events", [])
    if not events:
        f_big = font(18 * S, "w3")
        msg = "今天没有日程"
        d.text(((W * S - text_w(d, msg, f_big)) // 2, 118 * S), msg,
               font=f_big, fill=BLACK)
        footer(d, "今日 0 项", "", M)
        return finish(img)

    # 事件区是死的（44 ~ 212 可用，底下留一行给"还有 N 项"），每档都要算过：
    #   n * (标题行 + 副行 + 间距) 必须塞进去
    # gap 不低于 5px —— 反白块上下各占 4px，间距不够会压到邻居身上。
    n = len(events)
    if n <= 3:
        sz_time, sz_item, line_h, sub_h, gap = 16, 17, 20 * S, 14 * S, 18 * S
    elif n <= 4:
        sz_time, sz_item, line_h, sub_h, gap = 14, 15, 18 * S, 13 * S, 8 * S
    elif n <= 5:
        sz_time, sz_item, line_h, sub_h, gap = 13, 14, 16 * S, 12 * S, 6 * S
    elif n <= 6:
        sz_time, sz_item, line_h, sub_h, gap = 12, 13, 15 * S, 11 * S, 5 * S
    elif n <= 7:
        sz_time, sz_item, line_h, sub_h, gap = 12, 12, 14 * S, 10 * S, 4 * S
    else:
        # 事件太多了，副行先让位 —— 标题是底线信息
        sz_time, sz_item, line_h, sub_h, gap = 12, 13, 15 * S, 0, 4 * S

    f_time = font(sz_time * S, "w6")
    f_item = font(sz_item * S, "w3")
    f_item_b = font(sz_item * S, "w6")
    f_small = font(MIN_PX * S, "med")

    AXIS = M + 8 * S       # 竖直时间轴的 x
    TX = M + 26 * S       # 时间列（16px 的 "10:30" 宽 50，列宽要留够）
    CX = M + 88 * S       # 标题列
    maxw = W * S - CX - M

    ribbon_top = 230 * S
    limit = ribbon_top - 18 * S      # 底下留一行给"还有 N 项"

    prev_cy = None
    shown = 0
    for ev in events:
        title_lines = wrap(d, ev.get("title", ""), f_item, maxw)
        sub = []
        if ev.get("now"):
            sub.append("进行中")
            rem = _remaining(ev, now)
            if rem:
                sub.append(rem)
        elif ev.get("next"):
            sub.append("下一个")
            cd = _countdown(ev.get("start_ts"), now)
            if cd:
                sub.append(cd)
        if ev.get("end_hm"):
            sub.append("至 " + ev["end_hm"])
        if ev.get("location"):
            sub.append(ev["location"][:14])
        sub_text = " · ".join(sub) if sub_h else ""

        is_now = bool(ev.get("now"))
        is_next = bool(ev.get("next"))
        extra = 8 * S if is_now else 0     # 反白块自带呼吸空间

        block = line_h * len(title_lines) + (sub_h if sub_text else 0)
        if y + block + extra > limit:
            break

        if is_now:
            # 进行中：整块反白，左侧空心大圈
            y += 2 * S
            card = [M - 2 * S, y - 4 * S, W * S - M + 2 * S, y + block + 3 * S]
            d.rectangle(card, fill=BLACK)
            d.text((TX, y), ev.get("start_hm", ""), font=f_time, fill=WHITE)
            for i, ln in enumerate(title_lines):
                d.text((CX, y + i * line_h), ln,
                       font=f_item_b if i == 0 else f_item, fill=WHITE)
            if sub_text:
                d.text((CX, y + len(title_lines) * line_h), sub_text,
                       font=f_small, fill=WHITE)
            r = 7 * S
            cy = y + 7 * S
            d.ellipse([AXIS - r, cy - r, AXIS + r, cy + r],
                      outline=WHITE, width=2 * S)
            prev_cy = cy
            y += block + gap + extra
            shown += 1
            continue

        # 竖直轴：圆点到圆点连成一条线，贯穿事件块才像"时间轴"
        r = 3 * S
        cy = y + 6 * S
        if prev_cy is not None:
            d.line([(AXIS, prev_cy), (AXIS, cy)], fill=BLACK, width=S)
        d.ellipse([AXIS - r, cy - r, AXIS + r, cy + r], fill=BLACK)

        d.text((TX, y), ev.get("start_hm", ""), font=f_time, fill=BLACK)
        for i, ln in enumerate(title_lines):
            d.text((CX, y + i * line_h), ln,
                   font=f_item_b if (is_next and i == 0) else f_item, fill=BLACK)
        if sub_text:
            d.text((CX, y + len(title_lines) * line_h), sub_text,
                   font=f_small, fill=BLACK)
        prev_cy = cy
        y += block + gap
        shown += 1

    if shown < n:
        d.text((M, limit), "还有 %d 项" % (n - shown), font=f_small, fill=BLACK)

    # ---- 底部区域：两种方案二选一 ----
    if RIBBON_MODE == "gap":
        ribbon_gap(d, events, now, M, ribbon_top)
    else:
        ribbon_axis(d, events, now, M, ribbon_top)

    upd = state.get("updated_at", "")
    left = "今日 %d 项" % n
    if mock_tag(state, "events"):
        left += " · 演示数据"
    footer(d, left, (upd[-5:] + " 更新") if upd else "", M)
    return finish(img)


RENDERERS = {"standby": render_standby, "todo": render_todo, "schedule": render_schedule}


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"updated_at": "", "tasks": [], "events": [], "open_tasks": 0, "total_tasks": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--state", default=STATE)
    a = ap.parse_args()

    state = load_state()
    if a.state != STATE:
        with open(a.state, encoding="utf-8") as f:
            state = json.load(f)

    os.makedirs(OUT, exist_ok=True)
    modes = [a.mode] if a.mode else list(MODES)
    for m in modes:
        bw = RENDERERS[m](state)
        bw.save(os.path.join(OUT, m + ".png"))
        bw.convert("L").resize((W * 2, H * 2), Image.NEAREST).save(
            os.path.join(OUT, m + "_2x.png"))
        print("rendered %-9s -> out/v3/%s.png  (%d bytes)" % (m, m, len(pack(bw))))


if __name__ == "__main__":
    main()
