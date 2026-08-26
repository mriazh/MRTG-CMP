"""Pixel-perfect RRDtool-style traffic graph renderer matching telco MRTG."""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, Locator, NullLocator

from .config import settings
from .db import TrafficSample

WIB_OFFSET = timedelta(hours=7)
WIB_TZ = timezone(WIB_OFFSET, name="WIB")
GRID_MAJOR_COLOR = "#FFAAAA"
ARROW_COLOR = "#CC0000"
FIGURE_FACECOLOR = "#E5E5E5"
FONT_FAMILY = ["Courier New", "DejaVu Sans Mono", "monospace"]
BEZEL_HIGHLIGHT_COLOR = "#FFFFFF"
BEZEL_SHADE_COLOR = "#808080"
SUBTITLE_Y = 0.165
INBOUND_LEGEND_Y = 0.095
OUTBOUND_LEGEND_Y = 0.035


def _format_subtitle_datetime(value: str | datetime) -> str:
    """Format a range endpoint without a timezone suffix in the caption."""
    if isinstance(value, str):
        return value.strip().removesuffix(" WIB")
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_scaled_value(value: float, divisor: float, suffix: str) -> str:
    """Format a scaled rate with one decimal place."""
    return f"{value / divisor:.1f} {suffix}"


def nice_ceiling(value: float) -> float:
    """Round a rate up to an RRDtool-style magnitude and multiplier."""
    if value <= 0.0:
        return 100_000.0

    exponent = math.floor(math.log10(value))
    magnitude = float(10**exponent)
    fraction = value / magnitude
    for multiplier in (1.0, 2.0, 2.5, 4.0, 5.0, 8.0, 10.0):
        if fraction <= multiplier:
            return float(multiplier * magnitude)
    return 10.0 * magnitude


def format_engineering_bits(value: float, pos: Any = None) -> str:
    """Format bits per second into clean RRDtool engineering notation."""
    if abs(value) < 1e-9:
        return "0.0"
    abs_val = abs(value)
    if abs_val >= 1_000_000_000:
        return _format_scaled_value(value, 1_000_000_000, "G")
    elif abs_val >= 1_000_000:
        return _format_scaled_value(value, 1_000_000, "M")
    elif abs_val >= 1_000:
        return _format_scaled_value(value, 1_000, "k")
    else:
        return f"{value:.1f} b"


def format_megabits(value: float) -> str:
    """Format a rate in Megabits with the fixed two-decimal legend width."""
    return f"{value / 1_000_000:.2f} M"


def calculate_statistics(
    samples: Sequence[dict[str, Any] | TrafficSample],
) -> dict[str, dict[str, float]]:
    """Compute Current, Average, and Maximum bandwidth rates (in bps) for In/Out."""
    rx_rates: list[float] = []
    tx_rates: list[float] = []

    for s in samples:
        status = s["status"] if isinstance(s, dict) else s.status
        if status in ("UP", "RESET"):
            rx = float(s["rx_bps"] if isinstance(s, dict) else s.rx_bps)
            tx = float(s["tx_bps"] if isinstance(s, dict) else s.tx_bps)
            rx_rates.append(rx)
            tx_rates.append(tx)

    if not rx_rates:
        return {
            "in": {"current": 0.0, "average": 0.0, "maximum": 0.0},
            "out": {"current": 0.0, "average": 0.0, "maximum": 0.0},
        }

    return {
        "in": {
            "current": rx_rates[-1],
            "average": sum(rx_rates) / len(rx_rates),
            "maximum": max(rx_rates),
        },
        "out": {
            "current": tx_rates[-1],
            "average": sum(tx_rates) / len(tx_rates),
            "maximum": max(tx_rates),
        },
    }


def render_traffic_graph(
    samples: Sequence[dict[str, Any] | TrafficSample],
    title: str | None = None,
    start_time: str | datetime | None = None,
    end_time: str | datetime | None = None,
    start_epoch: int | float | None = None,
    end_epoch: int | float | None = None,
    width_px: int = 800,
    height_px: int = 340,
    dpi: int = 100,
    watermark: str = "RRDTOOL / TOBI OETIKER",
) -> bytes:
    """Render an authentic RRDtool-aesthetic traffic graph into PNG bytes."""
    if title is None:
        title = f"Traffic WAN ({settings.uplink_name}) - {settings.site_name}"
    figsize = (width_px / dpi, height_px / dpi)
    fig = plt.figure(figsize=figsize, dpi=dpi, facecolor=FIGURE_FACECOLOR)

    # Chiseled 3D bezel around the outer figure edge.
    for xdata, ydata, color in (
        ([0.0, 1.0], [1.0, 1.0], BEZEL_HIGHLIGHT_COLOR),
        ([0.0, 0.0], [0.0, 1.0], BEZEL_HIGHLIGHT_COLOR),
        ([0.0, 1.0], [0.0, 0.0], BEZEL_SHADE_COLOR),
        ([1.0, 1.0], [0.0, 1.0], BEZEL_SHADE_COLOR),
    ):
        fig.lines.append(
            mlines.Line2D(
                xdata,
                ydata,
                transform=fig.transFigure,
                color=color,
                linewidth=1.0,
                clip_on=False,
            )
        )

    # Plot area layout: left=0.10, bottom=0.25, width=0.84, height=0.61
    ax = fig.add_axes((0.10, 0.25, 0.84, 0.61))
    ax.set_facecolor("#FFFFFF")

    timestamps: list[datetime] = []
    rx_bps_list: list[float] = []
    tx_bps_list: list[float] = []

    for s in samples:
        epoch = s["epoch"] if isinstance(s, dict) else s.epoch
        status = s["status"] if isinstance(s, dict) else s.status
        rx_bps = s["rx_bps"] if isinstance(s, dict) else s.rx_bps
        tx_bps = s["tx_bps"] if isinstance(s, dict) else s.tx_bps

        if epoch is None:
            continue

        dt = datetime.fromtimestamp(epoch, tz=WIB_TZ)
        timestamps.append(dt)

        if status == "DOWN":
            rx_bps_list.append(float("nan"))
            tx_bps_list.append(float("nan"))
        else:
            rx_bps_list.append(float(rx_bps))
            tx_bps_list.append(float(tx_bps))

    stats = calculate_statistics(samples)

    peak_bps = max(
        (r for r in (*rx_bps_list, *tx_bps_list) if not math.isnan(r)),
        default=0.0,
    )
    y_max = nice_ceiling(peak_bps * 1.05)
    y_step = y_max / 5.0

    if timestamps:
        if start_epoch is not None and end_epoch is not None and end_epoch > start_epoch:
            st_dt = datetime.fromtimestamp(start_epoch, tz=WIB_TZ)
            et_dt = datetime.fromtimestamp(end_epoch, tz=WIB_TZ)
        else:
            st_dt = timestamps[0]
            et_dt = timestamps[-1]
    elif start_epoch is not None and end_epoch is not None and end_epoch > start_epoch:
        st_dt = datetime.fromtimestamp(start_epoch, tz=WIB_TZ)
        et_dt = datetime.fromtimestamp(end_epoch, tz=WIB_TZ)
    else:
        st_dt = datetime.fromtimestamp(0, tz=WIB_TZ)
        et_dt = st_dt

    span_seconds = (et_dt - st_dt).total_seconds()

    if timestamps:
        # Inbound: pure stepped green polygon without a contour stroke.
        ax.fill_between(
            timestamps,  # type: ignore[arg-type]
            0,
            rx_bps_list,
            color="#00CC00",
            edgecolor="none",
            step="post",
            alpha=1.0,
            zorder=2,
        )

        # Outbound: Solid Blue Line (#0000CC)
        ax.plot(
            timestamps,  # type: ignore[arg-type]
            tx_bps_list,
            color="#0000CC",
            linewidth=1.0,
            drawstyle="steps-post",
            zorder=4,
        )

        if span_seconds <= 900:
            locator: Locator = mdates.MinuteLocator(interval=1, tz=WIB_TZ)  # type: ignore[no-untyped-call]
            formatter = mdates.DateFormatter("%H:%M", tz=WIB_TZ)  # type: ignore[no-untyped-call]
        elif span_seconds <= 7200:
            locator = mdates.MinuteLocator(interval=10, tz=WIB_TZ)  # type: ignore[no-untyped-call]
            formatter = mdates.DateFormatter("%H:%M", tz=WIB_TZ)  # type: ignore[no-untyped-call]
        elif span_seconds <= 86400:
            locator = mdates.HourLocator(interval=2, tz=WIB_TZ)  # type: ignore[no-untyped-call]
            formatter = mdates.DateFormatter("%H:%M", tz=WIB_TZ)  # type: ignore[no-untyped-call]
        elif span_seconds <= 604800:
            locator = mdates.DayLocator(interval=1, tz=WIB_TZ)  # type: ignore[no-untyped-call]
            formatter = mdates.DateFormatter("%a %d", tz=WIB_TZ)  # type: ignore[no-untyped-call]
        else:
            locator = mdates.AutoDateLocator(  # type: ignore[no-untyped-call]
                minticks=6, maxticks=12, tz=WIB_TZ
            )
            formatter = mdates.DateFormatter("%b %d", tz=WIB_TZ)  # type: ignore[no-untyped-call]

        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xlim(st_dt, et_dt)  # type: ignore[arg-type]
        fig.autofmt_xdate(rotation=0, ha="center")
    else:
        if start_epoch is not None and end_epoch is not None and end_epoch > start_epoch:
            st_dt = datetime.fromtimestamp(float(start_epoch), tz=WIB_TZ)
            et_dt = datetime.fromtimestamp(float(end_epoch), tz=WIB_TZ)
            ax.set_xlim(st_dt, et_dt)  # type: ignore[arg-type]
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=WIB_TZ))  # type: ignore[no-untyped-call]

        ax.text(
            0.5,
            0.5,
            "No traffic data recorded in this range yet",
            horizontalalignment="center",
            verticalalignment="center",
            transform=ax.transAxes,
            color="#888888",
            fontsize=10,
            fontfamily=FONT_FAMILY,
        )

    # Styling axes matching RRDtool
    ax.set_ylabel(
        "bits per second",
        fontsize=9,
        color="#222222",
        labelpad=6,
        fontfamily=FONT_FAMILY,
    )
    ax.set_ylim(0.0, y_max)
    ax.set_yticks([i * y_step for i in range(round(y_max / y_step) + 1)])
    ax.yaxis.set_major_formatter(FuncFormatter(format_engineering_bits))
    ax.tick_params(
        axis="both",
        which="major",
        labelsize=8.5,
        colors="#333333",
        direction="out",
        length=3,
        labelfontfamily=FONT_FAMILY,
    )

    # RRDtool grid: dotted pink/red gridlines across all ticks.
    ax.minorticks_off()
    ax.grid(
        True,
        which="major",
        axis="both",
        linestyle=":",
        linewidth=0.8,
        color=GRID_MAJOR_COLOR,
        alpha=1.0,
        zorder=3,
    )
    ax.set_axisbelow(False)

    # Classic RRDtool frame with pink dotted top and right boundaries.
    ax.spines["left"].set_visible(True)
    ax.spines["bottom"].set_visible(True)
    ax.spines["top"].set_visible(True)
    ax.spines["right"].set_visible(True)
    ax.spines["top"].set_color(GRID_MAJOR_COLOR)
    ax.spines["top"].set_linestyle(":")
    ax.spines["top"].set_linewidth(0.8)
    ax.spines["right"].set_color(GRID_MAJOR_COLOR)
    ax.spines["right"].set_linestyle(":")
    ax.spines["right"].set_linewidth(0.8)
    for spine_name in ("left", "bottom"):
        ax.spines[spine_name].set_color("#666666")
        ax.spines[spine_name].set_linewidth(0.8)

    # Direction arrows at the top of Y and right of X.
    ax.add_patch(
        mpatches.FancyArrowPatch(
            (0, 0.98),
            (0, 1.0),
            transform=ax.transAxes,
            arrowstyle="-|>",
            mutation_scale=8,
            linewidth=0.8,
            color=ARROW_COLOR,
            clip_on=False,
        )
    )
    ax.add_patch(
        mpatches.FancyArrowPatch(
            (0.98, 0),
            (1.0, 0),
            transform=ax.transAxes,
            arrowstyle="-|>",
            mutation_scale=8,
            linewidth=0.8,
            color=ARROW_COLOR,
            clip_on=False,
        )
    )

    y_min, y_max = ax.get_ylim()
    ax.set_ylim(bottom=0, top=max(y_max, 1000.0))

    # Subtitle range caption
    if start_time and end_time:
        st_str = _format_subtitle_datetime(start_time)
        et_str = _format_subtitle_datetime(end_time)
        subtitle = f"From {st_str} To {et_str}"
    elif timestamps:
        t_start = timestamps[0].strftime("%Y-%m-%d %H:%M:%S")
        t_end = timestamps[-1].strftime("%Y-%m-%d %H:%M:%S")
        subtitle = f"From {t_start} To {t_end}"
    else:
        now_str = (datetime.now(UTC) + WIB_OFFSET).strftime("%Y-%m-%d %H:%M:%S")
        subtitle = f"No Data at {now_str} WIB"

    # Header Title (left-aligned, authentic RRDtool look)
    fig.text(
        0.03,
        0.94,
        title,
        fontsize=10.5,
        fontweight="bold",
        color="#111111",
        ha="left",
        fontfamily=FONT_FAMILY,
    )

    # Time range subtitle sits below X-axis labels and above the legend.
    fig.text(
        0.52,
        SUBTITLE_Y,
        subtitle,
        fontsize=8,
        color="#444444",
        ha="center",
        fontfamily=FONT_FAMILY,
    )

    # Watermark runs top-to-bottom along the rightmost bezel edge.
    if watermark:
        fig.text(
            1.0,
            0.55,
            watermark,
            fontsize=7,
            color="#888888",
            rotation=270,
            verticalalignment="center",
            horizontalalignment="right",
            fontfamily=FONT_FAMILY,
            fontweight="normal",
            clip_on=False,
        )

    # Legend / Statistics Table below plot (standardized to Megabits M with 2 decimals)
    fig.add_artist(
        mpatches.Rectangle(
            (0.0, 0.0),
            1.0,
            1.0,
            transform=fig.transFigure,
            fill=False,
            edgecolor="#808080",
            linewidth=1.0,
            clip_on=False,
        )
    )

    in_curr = format_megabits(stats["in"]["current"])
    in_avg = format_megabits(stats["in"]["average"])
    in_max = format_megabits(stats["in"]["maximum"])

    out_curr = format_megabits(stats["out"]["current"])
    out_avg = format_megabits(stats["out"]["average"])
    out_max = format_megabits(stats["out"]["maximum"])

    # Inbound Row (Solid green square)
    fig.patches.append(
        mpatches.Rectangle(
            (0.10, INBOUND_LEGEND_Y),
            0.02,
            0.038,
            transform=fig.transFigure,
            facecolor="#00CC00",
            edgecolor="black",
            linewidth=0.8,
        )
    )
    in_text = f"Inbound   Current: {in_curr:<9} Average: {in_avg:<9} Maximum: {in_max:<9}"
    fig.text(
        0.132,
        INBOUND_LEGEND_Y + 0.005,
        in_text,
        fontsize=8.5,
        fontfamily=FONT_FAMILY,
        color="#222222",
    )

    # Outbound Row (Solid blue square matching Telkom RRDtool)
    fig.patches.append(
        mpatches.Rectangle(
            (0.10, OUTBOUND_LEGEND_Y),
            0.02,
            0.038,
            transform=fig.transFigure,
            facecolor="#0000FF",
            edgecolor="black",
            linewidth=0.8,
        )
    )
    out_text = f"Outbound  Current: {out_curr:<9} Average: {out_avg:<9} Maximum: {out_max:<9}"
    fig.text(
        0.132,
        OUTBOUND_LEGEND_Y + 0.005,
        out_text,
        fontsize=8.5,
        fontfamily=FONT_FAMILY,
        color="#222222",
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=FIGURE_FACECOLOR)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


generate_traffic_graph = render_traffic_graph
