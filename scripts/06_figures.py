"""산출 표로 보고서 그림 3종 작성.

원자료를 다시 읽지 않고 04·05·07이 만든 outputs/tables만 씀. 파일 이름과 보고서 그림 번호는
fig1 = 그림 1, fig2 = 그림 2, fig4 = 그림 3. 한글 표기에 Noto Sans KR 글꼴이 필요함.

입력: outputs/tables/ 아래 a1_all.csv, extra_timeband.csv, extra_checks.csv, a1_bb_distance.csv,
      candidates_{장애인,경로}.csv, eligible_paths_{장애인,경로}.csv
출력: outputs/figures/fig1_gap_by_type.png, fig2_candidates.png, fig4_robustness.png(300dpi)
실행: python scripts/06_figures.py
"""

from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager

matplotlib.use("Agg")
ROOT = Path(__file__).resolve().parents[1]
TAB, FIG = ROOT / "outputs" / "tables", ROOT / "outputs" / "figures"

FONT = "Noto Sans KR"  # OFL 글꼴. macOS 사용자 글꼴 폴더의 OTF·TTF는 직접 등록하고 그 밖은 matplotlib 글꼴 목록에서 찾음
for f in (Path.home() / "Library" / "Fonts").glob("NotoSansKR*.[ot]tf"):
    font_manager.fontManager.addfont(str(f))
if FONT not in {f.name for f in font_manager.fontManager.ttflist}:
    raise SystemExit("Noto Sans KR 글꼴 필요: https://fonts.google.com/noto/specimen/Noto+Sans+KR")
plt.rcParams.update({"font.family": FONT, "font.size": 8, "axes.unicode_minus": False, "axes.edgecolor": "#b4b3ad",
                     "axes.linewidth": 0.6, "xtick.color": "#52514e", "ytick.color": "#52514e", "axes.labelcolor": "#0b0b0b",
                     "xtick.major.width": 0.6, "ytick.major.width": 0.6, "savefig.dpi": 300, "figure.dpi": 100})
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e6e5e1"
COLOR = {"장애인": "#2a78d6", "경로": "#eb6834", "어린이": "#8f8e8a", "청소년": "#8f8e8a"}
MARK = {"장애인": "o", "경로": "s", "어린이": "^", "청소년": "D"}
PERIOD = [("validation_prior", "8월"), ("main", "9월"), ("validation_later", "10월 1~13일")]
W = 4.7  # 그림 너비(인치). 보고서 본문 폭(약 4.96인치)에서 여백을 뺀 값


def style(ax):
    """x축 눈금 격자(세로선)를 깔고 위·오른쪽 테두리를 지우는 공통 축 스타일."""
    ax.grid(axis="x", color=GRID, lw=0.5)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def fig_gap_by_type():
    """그림 1. 왼쪽은 카드 유형별 버스→버스 차이(3개 기간), 오른쪽은 장애인·경로 카드의 시간대별 차이(추가 점검)."""
    a = pd.read_csv(TAB / "a1_all.csv")
    a = a[a.validity & (a.m == 5) & (a.stratum == "BB")]
    tb = pd.read_csv(TAB / "extra_timeband.csv")
    groups = ["장애인", "경로", "어린이", "청소년"]
    bands = ["출근(07~09시)", "낮(10~16시)", "퇴근(17~19시)", "저녁·심야(20~06시)"]
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.55), gridspec_kw={"wspace": 0.55})

    def mark(ax, r, y, g, main):
        ax.plot([r.ci_lo, r.ci_hi], [y, y], color=COLOR[g], lw=1.2 if main else 0.8, solid_capstyle="round")
        ax.plot(r.value, y, MARK[g], ms=5 if main else 3.6, mfc=COLOR[g] if main else "white", mec=COLOR[g], mew=0.9)

    for gi, g in enumerate(groups):
        for pi, (per, _) in enumerate(PERIOD):
            mark(axes[0], a[(a.group == g) & (a.period == per)].iloc[0], gi + (pi - 1) * 0.22, g, per == "main")
    for bi, band in enumerate(bands):
        for gi, g in enumerate(["장애인", "경로"]):
            for pi, (per, _) in enumerate(PERIOD):
                r = tb[(tb.group == g) & (tb.period == per) & (tb.band == band)].iloc[0]
                mark(axes[1], r, bi + (gi - 0.5) * 0.42 + (pi - 1) * 0.12, g, per == "main")
    for ax, labels, title in ((axes[0], groups, "카드 유형별 (버스→버스)"),
                              (axes[1], [b.replace("(", "\n(") for b in bands], "시간대별 (장애인●·경로■)")):
        ax.axvline(0, color=INK2, lw=0.6)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, fontsize=7)
        ax.set_ylim(len(labels) - 0.5, -0.6)
        ax.set_title(title, fontsize=8, color=INK, loc="left", pad=4)
        ax.set_xlabel("대상 카드 − 일반 카드 (분)")
        style(ax)
    h = [plt.Line2D([], [], marker="o", ls="", mfc=INK2, mec=INK2, ms=5, label="9월(주 분석)"),
         plt.Line2D([], [], marker="o", ls="", mfc="white", mec=INK2, ms=3.6, label="8월·10월(검증)")]
    fig.legend(handles=h, loc="lower center", ncol=2, frameon=False, fontsize=7.5, bbox_to_anchor=(0.5, 0.0))
    fig.subplots_adjust(left=0.1, right=0.98, top=0.9, bottom=0.29)
    fig.savefig(FIG / "fig1_gap_by_type.png")
    plt.close(fig)


def fig_candidates():
    """그림 2. 버스→버스 자격 경로의 차이와 대상 카드 기록 수(로그 축), 우선 확인 후보 10개에 순위 번호."""
    fig, axes = plt.subplots(1, 2, figsize=(W, 2.3), sharey=True, gridspec_kw={"wspace": 0.06})
    for ax, g in zip(axes, ["장애인", "경로"]):
        e = pd.read_csv(TAB / f"eligible_paths_{g}.csv", dtype={"a": str, "b": str})
        c = pd.read_csv(TAB / f"candidates_{g}.csv", dtype={"a": str, "b": str}).head(10)
        e = e[e.stratum.str.startswith("BB")]
        ax.scatter(e.N, e.M, s=5, color="#c9c8c2", lw=0, label="자격 버스 환승 경로")
        ax.scatter(c.N, c.M, s=22, color=COLOR[g], marker=MARK[g], edgecolor="white", lw=0.8, zorder=3,
                   label="우선 확인 후보 10")
        lx, placed = np.log10(c.N.to_numpy(float)), []
        for i, r in c.iterrows():  # 가까운 번호끼리 겹치면 아래쪽에 붙임
            near = any(abs(lx[i] - lx[j]) < 0.08 and abs(r.M - c.M[j]) < 0.5 for j in placed)
            ax.annotate(str(i + 1), (r.N, r.M), xytext=(3, -8 if near else 2), textcoords="offset points",
                        fontsize=6.5, color=INK)
            placed.append(i)
        ax.axhline(0, color=INK2, lw=0.6)
        ax.set_xscale("log")
        ax.set_xlabel(f"{g} 카드 환승 기록 수 (9월, 로그 축)")
        ax.set_title(f"{g} 카드", fontsize=8.5, loc="left", color=INK, pad=4)
        ax.grid(color=GRID, lw=0.5)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.legend(frameon=False, fontsize=6.5, loc="lower right", handletextpad=0.2)
    axes[0].set_ylabel("경로별 환승시간 차이 (분)")
    fig.subplots_adjust(left=0.1, right=0.98, top=0.9, bottom=0.17)
    fig.savefig(FIG / "fig2_candidates.png")
    plt.close(fig)


def fig_robustness():
    """그림 3. 버스→버스 차이를 점검 조건별로 한 축에 표시(점 = 값, 선 = 95% 구간).

    둘째 행만 경로 단위 부트스트랩 구간이고 나머지는 날짜 단위. 조건마다 경로-일을 다시 묶은 값을 씀.
    """
    a = pd.read_csv(TAB / "a1_all.csv")
    x = pd.read_csv(TAB / "extra_checks.csv")
    d = pd.read_csv(TAB / "a1_bb_distance.csv")

    def a1(g, per="main", validity=True):
        r = a[(a.group == g) & (a.period == per) & (a.stratum == "BB") & (a.m == 5) & (a.validity == validity)].iloc[0]
        return r.value, r.ci_lo, r.ci_hi

    def ex(g, check, subset):
        r = x[(x.group == g) & (x.check == check) & (x.subset == subset)].iloc[0]
        return r.value, r.ci_lo, r.ci_hi

    def dist(g, label):
        r = d[(d.group == g) & (d.period == "main") & (d.distance == label)].iloc[0]
        return r.value, r.ci_lo, r.ci_hi

    def path_ci(g):
        r = a[(a.group == g) & (a.period == "main") & (a.stratum == "BB") & (a.m == 5) & a.validity].iloc[0]
        return r.value, r.ci_path_lo, r.ci_path_hi

    def m_alt(g, m):
        r = a[(a.group == g) & (a.period == "main") & (a.stratum == "BB") & (a.m == m) & a.validity].iloc[0]
        return r.value, r.ci_lo, r.ci_hi

    rows = [("기준: 9월", lambda g: a1(g)),
            ("기준(경로 단위 구간)", path_ci),
            ("최소 가중치 1", lambda g: m_alt(g, 1)),
            ("최소 가중치 10", lambda g: m_alt(g, 10)),
            ("8월 자료", lambda g: a1(g, "validation_prior")),
            ("10월 1~13일 자료", lambda g: a1(g, "validation_later")),
            ("품질 규칙 미적용", lambda g: a1(g, validity=False)),
            ("상한(33·66분) 기록 제외", lambda g: ex(g, "상한 기록 제외", "전체")),
            ("시장·병원 등 정류장 제외", lambda g: ex(g, "정류장 이름 기준 시설(시장·상업·의료·복지)", "그 외")),
            ("같은 정류장 환승만", lambda g: dist(g, "같은 정류장")),
            ("300m 이하 정류장 사이만", lambda g: dist(g, "300m 이하")),
            ("평일만", lambda g: ex(g, "요일", "평일")),
            ("주말·공휴일만", lambda g: ex(g, "요일", "주말·공휴일"))]
    fig, ax = plt.subplots(figsize=(W, 3.1))
    for gi, g in enumerate(["장애인", "경로"]):
        for i, (_, f) in enumerate(rows):
            v, lo, hi = f(g)
            y = i + (gi - 0.5) * 0.3
            ax.plot([lo, hi], [y, y], color=COLOR[g], lw=1.0, solid_capstyle="round")
            ax.plot(v, y, MARK[g], ms=4.2, color=COLOR[g], mec="white", mew=0.6, label=f"{g} 카드" if i == 0 else None)
    ax.axvline(0, color=INK2, lw=0.6)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows])
    ax.set_ylim(len(rows) - 0.4, -0.6)
    ax.set_xlim(left=0 - 0.02)
    ax.set_xlabel("버스→버스 환승시간 차이(대상 카드 − 일반 카드, 분)")
    style(ax)
    ax.legend(frameon=False, fontsize=7, loc="lower left")
    fig.subplots_adjust(left=0.34, right=0.98, top=0.98, bottom=0.13)
    fig.savefig(FIG / "fig4_robustness.png")
    plt.close(fig)


if __name__ == "__main__":
    FIG.mkdir(parents=True, exist_ok=True)
    fig_gap_by_type()
    fig_candidates()
    fig_robustness()
