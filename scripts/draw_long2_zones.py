import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

plt.rcParams["font.family"] = "DejaVu Sans"
BG = "#fbfaf7"; INK = "#222"; MUTED = "#666"; GRID = "#999"

# Личные якоря Антона (zones.py, база 18.10.2026)
EASY, MAR, THR, INT, REP = 303, 260, 246, 225, 211      # 5:03 4:20 4:06 3:45 3:31
SLOW, FAST = 370, 210                                     # ось 6:10 … 3:30
MAR_BAND = round((EASY - MAR) / 4)                        # четверть зазора = 11 с
OVER_EDGE = 15

def fmt(s): return f"{s // 60}:{s % 60:02d}"
def x(p): return (SLOW - p) / (SLOW - FAST)

fig = plt.figure(figsize=(13.4, 11.2), dpi=100, facecolor=BG)
ax = fig.add_axes([0.025, 0.02, 0.95, 0.96]); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

def txt(xx, yy, s, size=13, color=INK, ha="left", weight="normal", va="center"):
    ax.text(xx, yy, s, fontsize=size, color=color, ha=ha, va=va, weight=weight, transform=ax.transAxes)

# Заголовок и ось
txt(0.0, 0.975, "Темп, мин/км — на цифрах Антона (18.10.2026). Слева медленно, справа быстро", 15, weight="bold")
ax.annotate("", xy=(1.0, 0.93), xytext=(0.0, 0.93), arrowprops=dict(arrowstyle="->", color=GRID, lw=1.6))
txt(0.0, 0.905, fmt(SLOW), 12, MUTED); txt(1.0, 0.905, fmt(FAST), 12, MUTED, ha="right")

# 1. Якоря
txt(0.0, 0.86, "1. Пять личных якорей (zones.py, из VO2max и ПАНО)", 15, weight="bold")
y0, h = 0.775, 0.055
ax.add_patch(FancyBboxPatch((0, y0), 1, h, boxstyle="round,pad=0.004,rounding_size=0.01",
                            fc="#f1efe9", ec="#555", lw=1.2, transform=ax.transAxes))
anchors = [(EASY, "лёгкий", 0), (MAR, "марафонский", 0), (THR, "пороговый = ПАНО", 1),
           (INT, "интервальный = МПК", 0), (REP, "повторный", 1)]
for p, name, row in anchors:
    ax.plot([x(p), x(p)], [y0 - 0.005, y0 + h + 0.005], color="#4a4ad8", lw=3, transform=ax.transAxes)
    yy = 0.74 - row * 0.065
    txt(x(p), yy, fmt(p), 14, ha="center"); txt(x(p), yy - 0.028, name, 12, MUTED, ha="center")

# 2. Зоны половины
txt(0.0, 0.575, "2. Пять зон половины для длительной (long2.py), границы от якорей", 15, weight="bold")
y1, h1 = 0.465, 0.075
b_slow, b_easy, b_mar, b_over = EASY + 45, EASY, MAR + MAR_BAND, THR   # 5:48, 5:03, 4:31, 4:06 (ПАНО медленнее, чем −15)
zones = [(SLOW, b_slow, "медл.", "#f1efe9", "#777"),
         (b_slow, b_easy, "лёгкая", "#e6f4ee", "#1d6b4f"),
         (b_easy, b_mar, "переходная", "#ebe8fb", "#4a4ad8"),
         (b_mar, b_over, "марафон.", "#fbe9e3", "#b4421f"),
         (b_over, FAST, "перебор", "#f1efe9", "#555")]
for a, b, name, fc, col in zones:
    xa, xb = x(a), x(b)
    ax.add_patch(FancyBboxPatch((xa + 0.002, y1), xb - xa - 0.004, h1, boxstyle="round,pad=0.002,rounding_size=0.01",
                                fc=fc, ec=col, lw=1.4, transform=ax.transAxes))
    txt((xa + xb) / 2, y1 + h1 / 2, name, 15 if (xb - xa) > 0.08 else 12, col, ha="center")
for p in (b_slow, b_easy, b_mar, b_over):
    ax.plot([x(p), x(p)], [y1 - 0.045, y1], color=GRID, lw=1, ls="--", transform=ax.transAxes)
txt(x(b_slow), 0.415, fmt(b_slow), 14, ha="center"); txt(x(b_slow), 0.388, "лёгкий +45 с", 12, MUTED, ha="center")
txt(x(b_easy), 0.415, fmt(b_easy), 14, ha="center"); txt(x(b_easy), 0.388, "лёгкий", 12, MUTED, ha="center")
txt(x(b_mar), 0.415, fmt(b_mar), 14, ha="center")
txt(x(b_mar), 0.388, f"марафонский + четверть зазора ({MAR_BAND} с)", 12, MUTED, ha="center")
txt(x(b_over), 0.355, fmt(b_over), 14, ha="center")
txt(x(b_over), 0.328, f"ПАНО; и всё быстрее марафонского на {OVER_EDGE} с ({fmt(MAR - OVER_EDGE)})", 12, MUTED, ha="center")
txt(0.0, 0.29, "Пометка «на границе … (N с)» — половина ближе 7 с/км к границе с марафонской, с перебором или к ПАНО; "
               "границы медл./лёгкая и лёгкая/переходная не помечаются.", 11.5, MUTED)

# 3. Типы
txt(0.0, 0.235, "3. Тип длительной = пара зон «первая половина → вторая»", 15, weight="bold")
left = ["I спокойная: вторая половина лёгкая (ровно; медл. → лёгкая; лёгкая → лёгкая)",
        "II прогресс: лёгкая → переходная",
        "III развивающая: переходная → переходная"]
right = ["IV быстрый финиш: лёгкая или переходная → марафонская",
         "V марафонская: марафонская → марафонская (особый случай, не еженедельный)",
         "вторая половина «медл.» — слишком медленно; любая половина «перебор» — не предлагаем"]
for i, s in enumerate(left):
    txt(0.0, 0.19 - i * 0.04, s, 13)
for i, s in enumerate(right):
    txt(0.0, 0.07 - i * 0.04, s, 13 if i < 2 else 12, INK if i < 2 else MUTED)
txt(0.0, -0.055, "Тип ставится «ползущим» проходом по лесенке от медленного к быстрому (long2.assign_types): "
                "переключается на следующий, когда вариант похож на него сильнее, чем на текущий.", 11.5, MUTED)

fig.savefig("docs/long2_zones.png", facecolor=BG, bbox_inches="tight", pad_inches=0.25)
print("saved")
