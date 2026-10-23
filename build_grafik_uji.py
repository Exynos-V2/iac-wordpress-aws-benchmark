"""Gambar 2 artikel: uji komparatif Terraform-Chef vs Terraform-Ansible.

Boxplot dengan titik setiap run, karena data tidak normal dan n kecil (rata-rata +/- SD
menyembunyikan sebaran dan outlier). Nilai dihitung ulang dari runs.csv (status Success)
memakai fungsi uji yang sama dengan scripts/runs.py: p Mann-Whitney eksak.
"""
import csv
import os
import random
import sys
from statistics import mean, median, stdev

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from runs import mann_whitney  # noqa: E402  (p eksak, permutasi eksak bila ada nilai kembar)

OUT = "grafik_uji_chef_ansible.png"
METRICS = [
    ("total_s", "Waktu total"),
    ("config_s", "Waktu konfigurasi"),
    ("tf_apply_s", "Waktu infrastruktur"),
]
GROUPS = [("Ansible", "Terraform-Ansible", "#8C8C8C"), ("Chef", "Terraform-Chef", "#D9D9D9")]
ALPHA = 0.05


def id_num(x, nd=2):
    """Format angka dengan koma desimal (gaya Indonesia)."""
    return f"{x:.{nd}f}".replace(".", ",")


rows = [r for r in csv.DictReader(open("runs.csv", encoding="utf-8")) if r["status"] == "Success"]


def values(stack, col):
    return [float(r[col]) for r in rows if r["stack"] == stack]


random.seed(7)  # jitter titik tetap sama setiap build
plt.rcParams.update({"font.family": "Times New Roman", "font.size": 9})
fig, axes = plt.subplots(1, 3, figsize=(6.6, 2.9), dpi=300)

for ax, (col, title) in zip(axes, METRICS):
    a, c = values("Ansible", col), values("Chef", col)
    # Chef sebagai kelompok pertama, sama dengan teks artikel (U = 7, 9, 45).
    u, p = mann_whitney(c, a)
    r = abs(1 - 2 * u / (len(a) * len(c)))  # korelasi rank-biserial
    pooled = (((len(a) - 1) * stdev(a) ** 2 + (len(c) - 1) * stdev(c) ** 2) / (len(a) + len(c) - 2)) ** 0.5
    d = abs(mean(a) - mean(c)) / pooled

    data = [a, c]
    bp = ax.boxplot(data, positions=[0, 1], widths=0.5, patch_artist=True, showfliers=False,
                    medianprops={"color": "black", "linewidth": 1.1},
                    whiskerprops={"linewidth": 0.7}, capprops={"linewidth": 0.7},
                    boxprops={"linewidth": 0.7})
    for patch, g in zip(bp["boxes"], GROUPS):
        patch.set_facecolor(g[2])
    for x, ys in enumerate(data):
        ax.scatter([x + random.uniform(-0.12, 0.12) for _ in ys], ys, s=7, color="black",
                   zorder=3, linewidths=0)

    lo, hi = min(a + c), max(a + c)
    span = hi - lo
    ax.set_ylim(lo - span * 0.12, hi + span * 0.75)
    for x, ys in enumerate(data):
        ax.text(x + 0.3, median(ys), id_num(median(ys)), ha="left", va="center", fontsize=6.5)

    p_txt = "p < 0,0001" if p < 0.0001 else f"p = {id_num(p, 4 if p < 0.01 else 3)}"
    verdict = "Chef signifikan lebih cepat" if p < ALPHA else "Tidak signifikan"
    ax.text(0.5, 0.97, f"U = {u:.0f}; {p_txt}\nd = {id_num(d)}; r = {id_num(r)}\n{verdict}",
            transform=ax.transAxes, ha="center", va="top", fontsize=7.5,
            fontweight="bold" if p < ALPHA else "normal")

    ax.set_title(title, fontsize=9.5, pad=4)
    ax.set_xticks([0, 1], ["Ansible", "Chef"])
    ax.set_xlim(-0.5, 1.75)
    ax.tick_params(axis="both", labelsize=8, length=2.5)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}"))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

axes[0].set_ylabel("Waktu (detik)", fontsize=9)
handles = [plt.Rectangle((0, 0), 1, 1, facecolor=g[2], edgecolor="black", linewidth=0.6) for g in GROUPS]
fig.legend(handles, [g[1] for g in GROUPS], loc="lower center", ncol=2, frameon=False,
           fontsize=8.5, bbox_to_anchor=(0.5, -0.01))
fig.tight_layout(rect=(0, 0.07, 1, 1), w_pad=1.6)
fig.savefig(OUT, dpi=300, facecolor="white")
print("WROTE", OUT)
