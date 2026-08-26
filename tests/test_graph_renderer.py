"""Unit tests for RRDtool-style traffic graph renderer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import matplotlib as mpl
import matplotlib.dates as mdates
import pytest
from matplotlib.colors import to_rgba
from matplotlib.text import Text
from matplotlib.ticker import NullLocator

import mrtg_cmp.graph_renderer as graph_renderer
from mrtg_cmp.graph_renderer import (
    calculate_statistics,
    format_engineering_bits,
    nice_ceiling,
    render_traffic_graph,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _assert_font_family(text: Text) -> None:
    assert text.get_fontfamily() == graph_renderer.FONT_FAMILY


def test_format_engineering_bits() -> None:
    """Verify one-decimal metric formatting with b, k, M, and G prefixes."""
    assert format_engineering_bits(0.0) == "0.0"
    assert format_engineering_bits(500.0) == "500.0 b"
    assert format_engineering_bits(10_000.0) == "10.0 k"
    assert format_engineering_bits(10_500.0) == "10.5 k"
    assert format_engineering_bits(7_630_000.0) == "7.6 M"
    assert format_engineering_bits(1_500_000_000.0) == "1.5 G"


def test_calculate_statistics() -> None:
    """Verify calculation of current, average, and maximum rates."""
    # Empty samples
    empty_stats = calculate_statistics([])
    assert empty_stats["in"]["current"] == 0.0
    assert empty_stats["out"]["average"] == 0.0

    samples = [
        {"epoch": 1000, "rx_bps": 4_000_000.0, "tx_bps": 2_000_000.0, "status": "UP"},
        {"epoch": 1300, "rx_bps": 8_000_000.0, "tx_bps": 4_000_000.0, "status": "UP"},
        {"epoch": 1600, "rx_bps": 0.0, "tx_bps": 0.0, "status": "DOWN"},  # Ignored in avg/max
        {"epoch": 1900, "rx_bps": 6_000_000.0, "tx_bps": 6_000_000.0, "status": "UP"},
    ]

    stats = calculate_statistics(samples)
    assert stats["in"]["current"] == 6_000_000.0
    # Average of 4M, 8M, 6M = 18M / 3 = 6M
    assert stats["in"]["average"] == 6_000_000.0
    assert stats["in"]["maximum"] == 8_000_000.0

    assert stats["out"]["current"] == 6_000_000.0
    # Average of 2M, 4M, 6M = 12M / 3 = 4M
    assert stats["out"]["average"] == 4_000_000.0
    assert stats["out"]["maximum"] == 6_000_000.0


def test_render_traffic_graph_empty_and_populated() -> None:
    """Ensure graph renderer produces valid PNG images for both empty and populated series."""
    # 1. Empty dataset
    png_empty = render_traffic_graph([])
    assert png_empty.startswith(PNG_MAGIC)
    assert len(png_empty) > 1000

    # 2. Populated dataset matching typical 24-hour sampling
    base_epoch = 1_700_000_000
    mock_samples = []
    for i in range(24):
        epoch = base_epoch + (i * 300)
        mock_samples.append(
            {
                "id": i + 1,
                "timestamp": datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "epoch": epoch,
                "rx_bytes": 100_000_000 + (i * 5_000_000),
                "tx_bytes": 50_000_000 + (i * 2_500_000),
                "rx_bps": 5_000_000.0 + ((i % 5) * 1_000_000.0),
                "tx_bps": 2_000_000.0,
                "status": "UP",
            }
        )

    png_populated = render_traffic_graph(
        mock_samples,
        title="Traffic WAN Main Uplink (150 Mbps) - Enterprise Gateway",
        start_time="2026-09-14 00:00:00",
        end_time="2026-09-14 23:55:00",
    )
    assert png_populated.startswith(PNG_MAGIC)
    assert len(png_populated) > 5000


@pytest.mark.parametrize(
    ("sample_rates", "expected_ticks"),
    [
        ([4_000_000.0, float("nan"), 3_000_000.0], [float(i * 1_000_000) for i in range(6)]),
        ([40_000_000.0, 20_000_000.0], [float(i * 10_000_000) for i in range(6)]),
    ],
)
def test_render_traffic_graph_uses_explicit_y_ticks(
    sample_rates: list[float], expected_ticks: list[float]
) -> None:
    """Verify deterministic Y-axis divisions for low and normal WAN traffic."""
    samples = [
        {
            "epoch": 1_700_000_000 + index * 60,
            "rx_bps": rate,
            "tx_bps": 1_000_000.0,
            "status": "DOWN" if rate != rate else "UP",
        }
        for index, rate in enumerate(sample_rates)
    ]

    graph_renderer.plt.close("all")
    with patch.object(graph_renderer.plt, "close"):
        graph_renderer.render_traffic_graph(samples)
        fig = graph_renderer.plt.gcf()
        fig.canvas.draw()
        ax = fig.axes[0]

        assert isinstance(ax.yaxis.get_major_locator(), mpl.ticker.FixedLocator)
        assert ax.get_ylim() == (0.0, expected_ticks[-1])
        assert ax.get_yticks().tolist() == expected_ticks
    graph_renderer.plt.close("all")


def test_render_traffic_graph_sets_every_minute_tick_for_subhour_range() -> None:
    """Verify sub-hour ranges retain every minute on the X-axis."""
    start = datetime(2026, 9, 14, 15, 42, tzinfo=graph_renderer.WIB_TZ)
    end = datetime(2026, 9, 14, 15, 50, tzinfo=graph_renderer.WIB_TZ)
    samples = [
        {
            "epoch": int((start + timedelta(minutes=offset)).timestamp()),
            "rx_bps": 4_000_000.0,
            "tx_bps": 2_000_000.0,
            "status": "UP",
        }
        for offset in range(9)
    ]
    expected_ticks = [start + timedelta(minutes=index) for index in range(9)]

    graph_renderer.plt.close("all")
    with patch.object(graph_renderer.plt, "close"):
        graph_renderer.render_traffic_graph(
            samples,
            start_epoch=int(start.timestamp()),
            end_epoch=int(end.timestamp()),
        )
        fig = graph_renderer.plt.gcf()
        fig.canvas.draw()
        ax = fig.axes[0]

        locator = ax.xaxis.get_major_locator()
        assert isinstance(locator, mdates.MinuteLocator)
        actual_ticks = [mdates.num2date(t, tz=start.tzinfo) for t in ax.get_xticks()]
        assert len(actual_ticks) == len(expected_ticks)
        for actual, expected in zip(actual_ticks, expected_ticks, strict=True):
            assert actual.replace(tzinfo=None) == expected.replace(tzinfo=None)
        assert ax.get_xlim() == (mdates.date2num(start), mdates.date2num(end))
    graph_renderer.plt.close("all")


def test_telkom_mrtg_stepped_rendering_and_legend_formatting() -> None:
    """Verify the framed, stepped, and legend styling of the rendered graph."""
    samples: list[dict[str, Any]] = [
        {"epoch": 1_700_000_000, "rx_bps": 2_000_000.0, "tx_bps": 1_000_000.0, "status": "UP"},
        {"epoch": 1_700_000_300, "rx_bps": 4_000_000.0, "tx_bps": 3_000_000.0, "status": "UP"},
    ]

    graph_renderer.plt.close("all")
    with patch.object(graph_renderer.plt, "close"):
        graph_renderer.render_traffic_graph(
            samples,
            start_time="2026-09-14 00:00:00 WIB",
            end_time="2026-09-14 23:59:59 WIB",
        )
        fig = graph_renderer.plt.gcf()
        fig.canvas.draw()

    try:
        ax = fig.axes[0]
        inbound_fill = ax.collections[0]
        fill_vertices = inbound_fill.get_paths()[0].vertices
        assert len(fill_vertices) == 9
        assert fill_vertices[1, 0] == fill_vertices[0, 0]
        assert fill_vertices[2, 1] == fill_vertices[1, 1] == 0.0
        assert fill_vertices[4, 1] == 4_000_000.0
        assert list(inbound_fill.get_facecolor()[0, :3]) == list(to_rgba("#00CC00")[:3])
        assert inbound_fill.get_edgecolor().size == 0

        assert len(ax.lines) == 1
        outbound = ax.lines[0]
        assert outbound.get_color().lower() == "#0000cc"
        assert outbound.get_drawstyle() == "steps-post"

        assert fig.get_facecolor()[:3] == to_rgba("#E5E5E5")[:3]
        assert ax.xaxis.get_minorticklocs().size == 0
        assert ax.yaxis.get_minorticklocs().size == 0
        assert ax.spines["top"].get_visible() is True
        assert ax.spines["right"].get_visible() is True
        assert ax.spines["left"].get_visible()
        assert ax.spines["bottom"].get_visible()

        expected_bezel = [
            ([0.0, 1.0], [1.0, 1.0], "#ffffff"),
            ([0.0, 0.0], [0.0, 1.0], "#ffffff"),
            ([0.0, 1.0], [0.0, 0.0], "#808080"),
            ([1.0, 1.0], [0.0, 1.0], "#808080"),
        ]
        assert len(fig.lines) == len(expected_bezel)
        for line, (xdata, ydata, color) in zip(fig.lines, expected_bezel, strict=True):
            assert list(line.get_xdata()) == xdata
            assert list(line.get_ydata()) == ydata
            assert line.get_color().lower() == color
            assert line.get_linewidth() == 1.0

        x_grid_lines = ax.xaxis.get_gridlines()
        y_grid_lines = ax.yaxis.get_gridlines()
        assert x_grid_lines
        assert y_grid_lines
        marked_x_positions = list(ax.xaxis.get_majorticklocs())
        x_grid_positions = [line.get_xdata()[0] for line in x_grid_lines]
        assert len(x_grid_lines) == len(marked_x_positions)
        assert all(
            any(abs(x - marked) < 1e-10 for x in x_grid_positions)
            for marked in marked_x_positions
        )
        assert graph_renderer.GRID_MAJOR_COLOR == "#FFAAAA"
        assert all(list(line.get_ydata()) == [0, 1] for line in x_grid_lines)
        assert all(line.get_color() == "#FFAAAA" for line in x_grid_lines)
        assert all(line.get_color() == "#FFAAAA" for line in y_grid_lines)
        assert all(line.get_linestyle() == ":" for line in x_grid_lines + y_grid_lines)
        assert all(line.get_linewidth() == 0.8 for line in x_grid_lines + y_grid_lines)
        assert all(line.get_alpha() == 1.0 for line in x_grid_lines + y_grid_lines)

        assert ax.get_axisbelow() is False
        assert inbound_fill.get_zorder() == 2
        assert outbound.get_zorder() == 4
        assert all(
            line.get_zorder() > inbound_fill.get_zorder()
            for line in x_grid_lines + y_grid_lines
        )
        assert all(line.get_zorder() == 3 for line in x_grid_lines + y_grid_lines)

        y_locator = ax.yaxis.get_major_locator()
        assert isinstance(y_locator, mpl.ticker.FixedLocator)
        expected_y_ticks = [
            0.0,
            1_000_000.0,
            2_000_000.0,
            3_000_000.0,
            4_000_000.0,
            5_000_000.0,
        ]
        assert ax.get_yticks().tolist() == expected_y_ticks

        assert len(ax.patches) == 2
        assert {patch.get_edgecolor()[:3] for patch in ax.patches} == {(0.8, 0.0, 0.0)}

        title = next(text for text in fig.texts if text.get_text().startswith("Traffic WAN"))
        subtitle = next(text for text in fig.texts if text.get_text().startswith("From "))
        inbound_legend = next(text for text in fig.texts if text.get_text().startswith("Inbound"))
        watermark = next(text for text in fig.texts if text.get_text() == "RRDTOOL / TOBI OETIKER")
        assert title.get_position()[0] == 0.03
        assert title.get_horizontalalignment() == "left"
        assert subtitle.get_text() == "From 2026-09-14 00:00:00 To 2026-09-14 23:59:59"
        assert subtitle.get_horizontalalignment() == "center"
        assert subtitle.get_position()[1] < ax.get_position().y0
        assert subtitle.get_window_extent().y1 < min(
            label.get_window_extent().y0 for label in ax.get_xticklabels() if label.get_visible()
        )
        assert subtitle.get_window_extent().y0 > inbound_legend.get_window_extent().y1

        legend_squares = fig.patches
        assert len(legend_squares) == 2
        assert legend_squares[0].get_facecolor()[:3] == (0.0, 0.8, 0.0)
        assert legend_squares[1].get_facecolor()[:3] == (0.0, 0.0, 1.0)
        assert all(patch.get_edgecolor()[:3] == (0.0, 0.0, 0.0) for patch in legend_squares)
        assert all(patch.get_linewidth() == 0.8 for patch in legend_squares)

        assert len(fig.artists) == 1
        outer_border = fig.artists[0]
        assert outer_border.get_edgecolor()[:3] == to_rgba("#808080")[:3]
        assert outer_border.get_linewidth() == 1.0

        assert watermark.get_rotation() == 270
        assert watermark.get_position()[0] == 1.0
        assert watermark.get_horizontalalignment() == "right"
        assert watermark.get_verticalalignment() == "center"
        assert watermark.get_fontfamily() == graph_renderer.FONT_FAMILY

        legend_texts = {text.get_text() for text in fig.texts}
        assert any("Inbound   Current: 4.00 M" in text for text in legend_texts)
        assert any("Outbound  Current: 3.00 M" in text for text in legend_texts)
    finally:
        graph_renderer.plt.close("all")


@pytest.mark.parametrize(
    ("peak_bps", "expected_ceiling"),
    [
        (2_000_000.0, 2_500_000.0),
        (6_400_000.0, 8_000_000.0),
        (40_000_000.0, 50_000_000.0),
        (80_000_000.0, 100_000_000.0),
        (1_500_000.0, 2_000_000.0),
    ],
)
def test_nice_ceiling_uses_rrdtool_steps(
    peak_bps: float, expected_ceiling: float
) -> None:
    """RRDtool-style ceiling with 105% headroom lands on clean magnitude steps."""
    assert nice_ceiling(peak_bps * 1.05) == float(expected_ceiling)


def test_nice_ceiling_handles_zero_and_negative() -> None:
    """Idle traffic floors at 100 kbps so the axis never collapses."""
    assert nice_ceiling(0.0) == 100_000.0
    assert nice_ceiling(-1.0) == 100_000.0


def _render_with_span(start: datetime, end: datetime) -> Any:
    """Render a graph for a synthetic span and return the axes."""
    samples = [
        {
            "epoch": int((start + delta).timestamp()),
            "rx_bps": 4_000_000.0,
            "tx_bps": 2_000_000.0,
            "status": "UP",
        }
        for delta in (timedelta(minutes=1), timedelta(minutes=2))
    ]
    graph_renderer.plt.close("all")
    with patch.object(graph_renderer.plt, "close"):
        graph_renderer.render_traffic_graph(
            samples,
            start_epoch=int(start.timestamp()),
            end_epoch=int(end.timestamp()),
        )
        fig = graph_renderer.plt.gcf()
        fig.canvas.draw()
        return fig.axes[0]
    return None


@pytest.mark.parametrize(
    ("delta", "expected_type"),
    [
        (timedelta(minutes=8), mdates.MinuteLocator),
        (timedelta(minutes=30), mdates.MinuteLocator),
        (timedelta(hours=1), mdates.MinuteLocator),
        (timedelta(hours=2), mdates.MinuteLocator),
        (timedelta(hours=12), mdates.HourLocator),
        (timedelta(hours=24), mdates.HourLocator),
        (timedelta(days=3), mdates.DayLocator),
        (timedelta(days=7), mdates.DayLocator),
        (timedelta(days=10), mdates.AutoDateLocator),
    ],
)
def test_adaptive_time_locators_across_timespans(
    delta: timedelta, expected_type: type
) -> None:
    """Each timespan tier selects the documented major locator type."""
    base = datetime(2026, 9, 14, 0, 0, tzinfo=graph_renderer.WIB_TZ)
    end = base + delta
    ax = _render_with_span(base, end)
    assert ax is not None
    assert isinstance(ax.xaxis.get_major_locator(), expected_type)
    assert isinstance(ax.xaxis.get_minor_locator(), NullLocator)
    graph_renderer.plt.close("all")
