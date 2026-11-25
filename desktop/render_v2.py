#!/usr/bin/env python3
"""
三屏渲染器：待机 / 待办 / 日程。

输入 state.json（lark_pull.py 产出，没飞书权限时用 mock_state.py 造），
输出 400x300 的 1-bit 位图。

排版全在这台 Mac 上做完（含中文），ESP32 只负责把字节贴到屏上 ——
在 MCU 上做 CJK 字体渲染是另一个量级的工程，不值得。

设计约束：只有黑白两色，所以层次靠「反白块 / 粗细分隔线 / 字重 / 留白」堆，
不靠颜色。

用法:
    python3 render.py                 # 三屏都渲一遍到 out/
    python3 render.py --mode todo     # 只渲 todo
"""
import argparse
import json
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "v2")
STATE = os.path.join(HERE, "state_v2.json")
W, H = 400, 300
S = 4                                  # 先在 4 倍尺寸上画，再缩回 1-bit，边缘更干净

BLACK, WHITE = 0, 255

# 字体系统：1-bit 屏上细笔画会被阈值化打散，所以小字一律用 Medium 兜底。
# 实测（400x300 真实 1-bit 化后）：11px 是可读下限；Hiragino W3 12px 起才稳；
# 任何 Light / 衬线体在小字号下都会碎成渣。
FONT_FACES = [
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2, "w6"),      # 粗，用于主视觉
    ("/System/Library/Fonts/STHeiti Medium.ttc", 1, "med"),      # 中粗，小字用它
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0, "w3"),      # 常规，只用于 >=13px
    ("/Library/Fonts/Arial Unicode.ttf", 0, "sans"),              # 兜底
]
_cache = {}

WEEK = "一二三四五六日"
MODES = ("standby", "todo", "schedule")
MIN_PX = 11                                            # 小于这个字号在 1-bit 上不可读


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


def seg_bar(d, x, y, w, h, done, total, gap=2 * S, invert=False):
    """分段进度条：一格一个任务，实心=已完成。

    invert=True 用于黑底上：已完成画白块，未完成画黑块配白框。
    白底黑底两种环境共用同一个零件，版面才统一。
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
    """手绘感的勾选框：实心方块 + 一道白勾。"""
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
    """页头：反白竖条 + 栏目名，右侧时间，下面一条粗线。返回下一行 y。

    v2：三屏共用同一套页头，切换时"这是一套东西"的感觉才成立。
    """
    f = font(16 * S, "w6")
    bar = 4 * S
    d.rectangle([M, M + 2 * S, M + bar, M + 18 * S], fill=BLACK)
    d.text((M + bar + 6 * S, M - 1 * S), title, font=f, fill=BLACK)
    t = now.strftime("%H:%M")
    ft = font(14 * S, "med")
    d.text((W * S - M - text_w(d, t, ft), M + 1 * S), t, font=ft, fill=BLACK)
    y = M + 24 * S
    rule(d, M, y, W * S - M, 2)
    return y + 9 * S


def group_tag(d, x, y, s, h=15):
    """反白小标签：黑底白字的方块块，宽度贴合文字。

    1-bit 屏上这是最强的层级手段 —— 比加粗、比放大都管用，
    因为整屏只有黑白，"底色反转"天然就是"这里是一个分区"。
    """
    f = font(MIN_PX * S, "med")
    tw = text_w(d, s, f)
    w = tw + 10 * S
    d.rectangle([x, y, x + w, y + h * S], fill=BLACK)
    d.text((x + 5 * S, y + 2 * S), s, font=f, fill=WHITE)
    return w


def footer(d, left, right, M):
    """页脚：细分隔线 + 左右两段小字。"""
    fy = H * S - 17 * S
    rule(d, M, fy - 4 * S, W * S - M, 1)
    fs = font(MIN_PX * S, "med")
    d.text((M, fy), left, font=fs, fill=BLACK)
    d.text((W * S - M - text_w(d, right, fs), fy), right, font=fs, fill=BLACK)


def notice_card(d, M, top, width, title, detail):
    """未授权 / 异常提示：整块反白。

    1-bit 屏上「没有待办」和「待办没权限」长得一模一样（都是空列表）。
    所以这一路失败时必须显式画出来，不能让空屏自己解释。
    """
    f_t = font(16 * S, "w6")
    f_d = font(MIN_PX * S, "med")
    h = 56 * S
    d.rectangle([M, top, M + width, top + h], fill=BLACK)
    d.text((M + 12 * S, top + 8 * S), title, font=f_t, fill=WHITE)
    d.text((M + 12 * S, top + 32 * S), detail, font=f_d, fill=WHITE)


def err_note(state, key, what):
    """(state, key) -> (标题, 副标题) 或 None。

    只在这一路**确实没数据**时才提示。
    ok_tasks=True 但数据是 mock 补的 —— 那是"演示数据"，不是"未授权"，
    两者在屏上必须区分开。
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
# 待机
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
    M = 8 * S

    # 大时钟：唯一的主角
    f_clock = font(70 * S, "w6")
    t = now.strftime("%H:%M")
    d.text(((W * S - text_w(d, t, f_clock)) // 2, 16 * S), t, font=f_clock, fill=BLACK)

    # 日期：字距拉开当副标题
    f_d = font(12 * S, "med")
    ds = "%d月%d日  星期%s" % (now.month, now.day, WEEK[now.weekday()])
    dw = text_w(d, ds, f_d) + int(12 * S * 0.3) * (len(ds) - 1)
    track(d, ((W * S - dw) // 2, 100 * S), ds, f_d, BLACK, int(12 * S * 0.3))

    rule(d, M, 126 * S, W * S - M, 1)

    # 下一个日程：标题左，倒计时右，一行放下
    y = 137 * S
    nxt = state.get("next_event")
    label(d, (M, y), "下一个日程", MIN_PX, BLACK, 2 * S)
    y += 15 * S
    if nxt:
        fn = font(16 * S, "w6")
        f_sm = font(MIN_PX * S, "med")
        when = nxt.get("start_hm", "")
        wt = 0
        if when:
            d.text((M, y), when, font=fn, fill=BLACK)
            wt = text_w(d, when, fn)
        d.text((M + wt + 8 * S, y), nxt.get("title", "")[:13], font=fn, fill=BLACK)
        cd = _countdown(nxt.get("start_ts") or nxt.get("start"), now)
        if cd:
            d.text((W * S - M - text_w(d, cd, f_sm), y + 4 * S), cd,
                   font=f_sm, fill=BLACK)
    else:
        d.text((M, y), "今天没有日程", font=font(14 * S, "w3"), fill=BLACK)
    y += 28 * S

    rule(d, M, y, W * S - M, 1)

    # 今日概览：整块反白 —— 1-bit 上最重的元素，用来收住版面下半场
    y += 10 * S
    card_h = 60 * S
    d.rectangle([M, y, W * S - M, y + card_h], fill=BLACK)
    bx = M + 12 * S
    f_ov = font(15 * S, "w6")
    f_sm = font(MIN_PX * S, "med")

    total = state.get("total_tasks", 0)
    done = total - state.get("open_tasks", 0)
    n_ev = len(state.get("events", []))
    d.text((bx, y + 9 * S), "今日 %d 场会" % n_ev, font=f_ov, fill=WHITE)

    if state.get("ok_tasks") is False:
        d.text((bx, y + 32 * S), "待办未授权", font=f_sm, fill=WHITE)
    else:
        tag = " 演示" if mock_tag(state, "tasks") else ""
        d.text((bx, y + 32 * S), "待办 %d / %d 已完成%s" % (done, total, tag),
               font=f_sm, fill=WHITE)
        seg_bar(d, bx, y + 47 * S, W * S - 2 * M - 24 * S, 5 * S,
                done, min(total, 20) or 1, invert=True)

    # 底部反白条：像真设备的按键说明
    band_y = H * S - 26 * S
    d.rectangle([0, band_y, W * S, H * S], fill=BLACK)
    fb = font(MIN_PX * S, "med")
    hints = ["待机", "待办", "日程"]
    cw = (W * S) // 3
    for i, h in enumerate(hints):
        cx = cw * i + 16 * S
        d.rectangle([cx, band_y + 9 * S, cx + 7 * S, band_y + 16 * S],
                    outline=WHITE, width=S)
        d.text((cx + 11 * S, band_y + 8 * S), h, font=fb, fill=WHITE)
    return finish(img)


# ---------------------------------------------------------------------------
# 今日时间轴
# ---------------------------------------------------------------------------
def _span(ev):
    """事件的起止时间戳。缺 end 就按 30 分钟算。"""
    st = ev.get("start_ts", ev.get("start"))
    en = ev.get("end_ts", ev.get("end"))
    if st is None:
        return None
    if en is None:
        en = st + 1800
    if en < st:
        en = st + 600
    return st, en


def _floor_hour(ts):
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day, d.hour, 0, 0).timestamp()


def _ceil_hour(ts):
    h = _floor_hour(ts)
    return h if abs(h - ts) < 1 else h + 3600


def time_ribbon(d, events, now, M, top, width):
    """横向时间带：事件按真实时长落位，一眼看出日程密度、空隙和当前时刻。

    1-bit 上没有颜色，层次靠这几招：
      普通事件 = 实心黑条
      进行中   = 条上挖一个白块（反白），全屏最醒目
      下一个   = 条左端一个白点
      现在时刻 = 贯穿的粗竖线 + 三角标
    """
    spans = []
    for ev in events:
        sp = _span(ev)
        if sp:
            spans.append((sp[0], sp[1], ev))
    if not spans:
        return None

    t0 = _floor_hour(min(s[0] for s in spans)) - 1800
    t1 = _ceil_hour(max(s[1] for s in spans)) + 1800
    # 窗口必须盖住"现在"，否则看不到离下一场还有多久
    nf = _floor_hour(now.timestamp()) - 1800
    if nf < t0:
        t0 = nf
    if now.timestamp() + 3600 > t1:
        t1 = now.timestamp() + 3600
    if t1 - t0 < 6 * 3600:                       # 事件挤在一起时给足跨度
        mid = _floor_hour((min(s[0] for s in spans) +
                           max(s[1] for s in spans)) / 2)
        t0, t1 = mid - 3 * 3600, mid + 3 * 3600
    span = t1 - t0

    def x_of(t):
        return M + (t - t0) / span * width

    label(d, (M, top), "今日时间轴", MIN_PX, BLACK, 2 * S)

    axis_y = top + 28 * S
    bh = 13 * S
    half = bh // 2

    rule(d, M, axis_y, M + width, 1)

    # 刻度：跨度小按一小时，大按两小时
    step = 3600 if span <= 9 * 3600 else 7200
    f_tick = font(MIN_PX * S, "med")
    t = _ceil_hour(t0)
    while t <= t1:
        x = x_of(t)
        d.line([(x, axis_y - half - 2 * S), (x, axis_y + half + 2 * S)],
               fill=BLACK, width=S)
        lab = datetime.fromtimestamp(t).strftime("%H")
        d.text((x - text_w(d, lab, f_tick) // 2, axis_y + half + 5 * S),
               lab, font=f_tick, fill=BLACK)
        t += step

    # 事件条
    for st, en, ev in spans:
        x0 = x_of(st)
        x1 = x_of(en)
        if x1 - x0 < 3 * S:
            x1 = x0 + 3 * S
        d.rectangle([x0, axis_y - half, x1, axis_y + half], fill=BLACK)
        if ev.get("now"):
            d.rectangle([x0 + 2 * S, axis_y - half + 2 * S,
                         x1 - 2 * S, axis_y + half - 2 * S], fill=WHITE)
        elif ev.get("next"):
            r = 2 * S
            d.ellipse([x0 + 2 * S, axis_y - r, x0 + 4 * S + r, axis_y + r], fill=WHITE)

    # 现在时刻
    tn = now.timestamp()
    if t0 <= tn <= t1:
        x = x_of(tn)
        d.line([(x, axis_y - half - 5 * S), (x, axis_y + half + 2 * S)],
               fill=BLACK, width=2 * S)
        d.polygon([(x - 3 * S, axis_y - half - 9 * S),
                   (x + 3 * S, axis_y - half - 9 * S),
                   (x, axis_y - half - 4 * S)], fill=BLACK)

    # 摘要行：进行中 / 下一场
    sy = axis_y + half + 19 * S
    f_s = font(MIN_PX * S, "med")
    cur_ev = next((ev for _, _, ev in spans if ev.get("now")), None)
    nxt_ev = next((ev for _, _, ev in spans if ev.get("next")), None)
    if cur_ev:
        txt = "进行中 · %s" % cur_ev.get("title", "")[:16]
    elif nxt_ev:
        txt = "下一场 %s %s" % (nxt_ev.get("start_hm", ""),
                                nxt_ev.get("title", "")[:14])
    else:
        txt = "今天没有更多日程"
    d.text((M, sy), txt, font=f_s, fill=BLACK)
    nowlab = "现在 %s" % now.strftime("%H:%M")
    d.text((M + width - text_w(d, nowlab, f_s), sy), nowlab, font=f_s, fill=BLACK)
    return sy


# ---------------------------------------------------------------------------
# 待办
# ---------------------------------------------------------------------------
def _bucket(item, today_md):
    """把一条待办归进 逾期 / 今天 / 明天 / 以后。

    逾期单独一组是因为它是唯一"现在就该动手"的，混在列表里会被划过去。
    """
    due = (item.get("due") or "").strip()
    if not due:
        return "later"
    if due.startswith("今天"):
        return "today"
    if due.startswith("明天"):
        return "tomorrow"
    if due.startswith("昨天"):
        return "overdue"
    if len(due) >= 5 and due[2] == "-":
        return "overdue" if due[:5] < today_md else "later"
    return "later"


def render_todo(state, now=None):
    now = now or datetime.now()
    img, d = new_canvas()
    M = 8 * S

    y = header(d, "待办", now, M)

    # 这一路没拉到？画出来，别让它看起来像"没有待办"
    note = err_note(state, "tasks", "待办")
    if note:
        notice_card(d, M, y + 30 * S, W * S - 2 * M, note[0], note[1])
        footer(d, "待办清单", "未授权", M)
        return finish(img)

    items = state.get("tasks", [])
    done = sum(1 for i in items if i.get("done"))
    total = len(items)
    open_items = [i for i in items if not i.get("done")]

    # 进度条 + 计数
    seg_bar(d, M, y, W * S - 2 * M - 46 * S, 7 * S, done, min(total, 20) or 1)
    fc = font(12 * S, "w6")
    ct = "%d/%d" % (done, total)
    d.text((W * S - M - text_w(d, ct, fc), y - 3 * S), ct, font=fc, fill=BLACK)
    y += 16 * S

    f_item = font(15 * S, "w3")
    f_item_b = font(15 * S, "w6")
    f_small = font(MIN_PX * S, "med")
    row_gap = 5 * S
    maxw = W * S - 2 * M - 56 * S
    footer_y = H * S - 21 * S

    if not open_items:
        d.text((M, y + 24 * S), "全部完成", font=font(17 * S, "w6"), fill=BLACK)
        d.text((M, y + 48 * S), "共 %d 项" % total, font=f_small, fill=BLACK)
        footer(d, "待办清单", "已清空", M)
        return finish(img)

    # v2：按到期分组。已完成的不逐条列 —— 屏是用来决定"现在做什么"的，
    # 已完成的沉在进度条里就够。
    today_md = now.strftime("%m-%d")
    for key, title in (("overdue", "逾期"), ("today", "今天"),
                       ("tomorrow", "明天"), ("later", "以后")):
        group = [i for i in open_items if _bucket(i, today_md) == key]
        if not group:
            continue
        if y + 16 * S > footer_y:
            break
        group_tag(d, M, y, title)
        y += 21 * S
        for it in group:
            lines = wrap(d, it.get("text", ""), f_item, maxw)
            line_h = 19 * S
            block = line_h * len(lines)
            if y + block > footer_y:
                d.text((M, y), "还有 %d 项" % (len(group) - group.index(it)),
                       font=f_small, fill=BLACK)
                break
            bs = 13 * S
            checkbox(d, M, y + 2 * S, bs, False)
            # 高优先级：文字前一个实心方点
            tx = M + bs + 8 * S
            if it.get("pri") == "high":
                d.rectangle([tx, y + 6 * S, tx + 5 * S, y + 11 * S], fill=BLACK)
                tx += 10 * S
            for i, ln in enumerate(lines):
                f = f_item_b if i == 0 else f_item
                d.text((tx, y + i * line_h), ln, font=f, fill=BLACK)
            if it.get("due"):
                due = it["due"]
                d.text((W * S - M - text_w(d, due, f_small), y + 4 * S), due,
                       font=f_small, fill=BLACK)
            y += block + row_gap

    upd = state.get("updated_at", "")
    left = "待办清单 · 已完成 %d" % done
    if mock_tag(state, "tasks"):
        left += " · 演示数据"
    footer(d, left, (upd[-5:] + " 更新") if upd else "", M)
    return finish(img)


# ---------------------------------------------------------------------------
# 日程
# ---------------------------------------------------------------------------
def render_schedule(state, now=None):
    now = now or datetime.now()
    img, d = new_canvas()
    M = 8 * S

    y = header(d, "日程", now, M)

    note = err_note(state, "events", "日程")
    if note:
        notice_card(d, M, y + 30 * S, W * S - 2 * M, note[0], note[1])
        footer(d, "今日日程", "未授权", M)
        return finish(img)

    events = state.get("events", [])
    footer_y = H * S - 21 * S
    list_y = 182 * S          # 列表到此为止，下面留给时间轴

    if not events:
        f_big = font(18 * S, "w3")
        msg = "今天没有日程"
        d.text(((W * S - text_w(d, msg, f_big)) // 2, 118 * S), msg, font=f_big, fill=BLACK)
        return finish(img)

    # v2：按事件数量选一档尺寸。事件少时把版面撑满（否则中间空一大块），
    # 事件多时收紧。一套固定字号在两种极端下都不好看。
    n = len(events)
    if n <= 2:
        sz_time, sz_item, line_h, sub_h, gap = 16, 18, 24 * S, 16 * S, 18 * S
    elif n <= 4:
        sz_time, sz_item, line_h, sub_h, gap = 14, 16, 19 * S, 14 * S, 12 * S
    elif n <= 6:
        sz_time, sz_item, line_h, sub_h, gap = 13, 14, 16 * S, 12 * S, 7 * S
    else:
        sz_time, sz_item, line_h, sub_h, gap = 12, 13, 15 * S, 11 * S, 5 * S

    f_time = font(sz_time * S, "w6")
    f_time_l = font(sz_time * S, "w3")
    f_item = font(sz_item * S, "w3")
    f_item_b = font(sz_item * S, "w6")
    f_small = font(MIN_PX * S, "med")

    AXIS_X = M + 46 * S
    TX = M + 56 * S
    maxw = W * S - TX - M

    for ev in events:
        title_lines = wrap(d, ev.get("title", ""), f_item, maxw)
        sub = []
        if ev.get("now"):
            sub.append("进行中")
        elif ev.get("next"):
            sub.append("下一个")
        if ev.get("end_hm"):
            sub.append("至 " + ev["end_hm"])
        if ev.get("location"):
            sub.append(ev["location"][:14])
        sub_text = " · ".join(sub)

        block = line_h * len(title_lines) + (sub_h if sub_text else 0)
        if y + block > list_y:
            d.text((M, y), "还有 %d 项" % (len(events) - events.index(ev)),
                   font=f_small, fill=BLACK)
            break

        is_now = bool(ev.get("now"))
        is_next = bool(ev.get("next"))

        if is_now:
            # 进行中的日程整块反白 —— 全屏唯一的重点
            card = [M - 4 * S, y - 5 * S, W * S - M + 4 * S, y + block - 2 * S]
            d.rectangle(card, fill=BLACK)
            y += 2 * S
            d.text((M, y), ev.get("start_hm", ""), font=f_time, fill=WHITE)
            for i, ln in enumerate(title_lines):
                d.text((TX, y + i * line_h), ln,
                       font=f_item_b if i == 0 else f_item, fill=WHITE)
            if sub_text:
                d.text((TX, y + len(title_lines) * line_h), sub_text,
                       font=f_small, fill=WHITE)
            # 轴上的白点
            r = 4 * S
            cy = y + 6 * S
            d.ellipse([AXIS_X - r, cy - r, AXIS_X + r, cy + r], fill=WHITE)
            y += block + gap
            continue

        # 时间轴
        d.line([(AXIS_X, y), (AXIS_X, y + block - 4 * S)], fill=BLACK, width=S)
        r = 5 * S if is_next else 3 * S
        cy = y + 6 * S
        if is_next:
            d.ellipse([AXIS_X - r, cy - r, AXIS_X + r, cy + r], outline=BLACK, width=S)
        else:
            d.ellipse([AXIS_X - r, cy - r, AXIS_X + r, cy + r], fill=BLACK)

        d.text((M, y), ev.get("start_hm", ""),
               font=f_time if is_next else f_time_l, fill=BLACK)
        for i, ln in enumerate(title_lines):
            d.text((TX, y + i * line_h), ln,
                   font=f_item_b if is_next else f_item, fill=BLACK)
        if sub_text:
            d.text((TX, y + len(title_lines) * line_h), sub_text, font=f_small, fill=BLACK)
        y += block + gap

    # 今日时间轴：占住下半屏，把"什么时候有空"直接画出来
    time_ribbon(d, events, now, M, 190 * S, W * S - 2 * M)

    upd = state.get("updated_at", "")
    left = "今日 %d 项" % len(events)
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
        print("rendered %-9s -> out/%s.png  (%d bytes)" % (m, m, len(pack(bw))))


if __name__ == "__main__":
    main()
