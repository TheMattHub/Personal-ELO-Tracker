/* Lunchbreak ELO — chart engine (vanilla SVG, theme-aware, no dependencies)
   Charts read their colors from CSS custom properties at render time, so the
   validated categorical palette swaps automatically with the light/dark theme. */
(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  const el = (name, attrs, parent) => {
    const node = document.createElementNS(SVG_NS, name);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  };

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const seriesColor = (index) => cssVar(`--series-${(index % 8) + 1}`) || "#3987e5";

  const fmtDelta = (delta) => {
    const value = Math.round(delta * 10) / 10;
    if (value > 0) return `+${value}`;
    return `${value}`;
  };

  const niceTicks = (min, max, count) => {
    const span = max - min;
    if (span <= 0) return [min];
    const step = Math.pow(10, Math.floor(Math.log10(span / count)));
    const err = (count * step) / span;
    let niceStep = step;
    if (err <= 0.15) niceStep = step * 10;
    else if (err <= 0.35) niceStep = step * 5;
    else if (err <= 0.75) niceStep = step * 2;
    const ticks = [];
    for (let v = Math.ceil(min / niceStep) * niceStep; v <= max + 1e-9; v += niceStep) {
      ticks.push(Math.round(v * 100) / 100);
    }
    return ticks;
  };

  const shortDate = (date, stripYear) => {
    if (stripYear && /^\d{4}-\d{2}-\d{2}/.test(date)) return date.slice(5, 10);
    return date;
  };

  /* ------------------------------------------------------------------ */
  /* Multi-series rating line chart                                      */
  /* ------------------------------------------------------------------ */
  const renderLineChart = (root, data) => {
    root.textContent = "";
    const width = Math.max(root.clientWidth || root.parentElement.clientWidth || 640, 320);
    const isNarrow = width < 560;
    const height = Math.min(340, Math.max(230, Math.round(width * 0.42)));
    const pad = { top: 14, right: 18, bottom: 30, left: 46 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;

    const matches = data.matches;
    const totalX = matches.length;
    let minY = Infinity;
    let maxY = -Infinity;
    data.series.forEach((s) => s.points.forEach((p) => {
      if (p.r < minY) minY = p.r;
      if (p.r > maxY) maxY = p.r;
    }));
    if (!isFinite(minY)) return;
    if (minY === maxY) { minY -= 25; maxY += 25; }
    const padY = Math.max(10, (maxY - minY) * 0.1);
    minY -= padY;
    maxY += padY;

    const x = (i) => pad.left + (totalX <= 1 ? plotW / 2 : (i / (totalX - 1)) * plotW);
    const y = (r) => pad.top + ((maxY - r) / (maxY - minY)) * plotH;

    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      role: "img",
      tabindex: "0",
      "aria-label": root.dataset.chartLabel || "",
    });

    /* Grid + y axis */
    niceTicks(minY, maxY, 4).forEach((tick) => {
      const ty = y(tick);
      if (ty < pad.top - 1 || ty > pad.top + plotH + 1) return;
      el("line", { x1: pad.left, y1: ty, x2: pad.left + plotW, y2: ty, class: "chart-grid-line" }, svg);
      el("text", { x: pad.left - 8, y: ty + 3.5, "text-anchor": "end", class: "chart-axis-text" }, svg)
        .textContent = String(Math.round(tick));
    });
    el("line", { x1: pad.left, y1: pad.top + plotH, x2: pad.left + plotW, y2: pad.top + plotH, class: "chart-axis-line" }, svg);

    /* X ticks */
    const sameYear = matches.every((m) => String(m.date).slice(0, 4) === String(matches[0].date).slice(0, 4));
    const maxLabels = Math.max(3, Math.floor(plotW / (isNarrow ? 72 : 86)));
    const step = Math.max(1, Math.ceil(totalX / maxLabels));
    for (let i = 0; i < totalX; i += step) {
      const index = (i + step >= totalX && i !== totalX - 1) ? totalX - 1 : i;
      el("text", { x: x(index), y: height - 8, "text-anchor": "middle", class: "chart-axis-text" }, svg)
        .textContent = shortDate(String(matches[index].date), sameYear);
      if (index === totalX - 1) break;
    }

    /* Series */
    data.series.forEach((series, index) => {
      const color = seriesColor(index);
      const pointsAttr = series.points.map((p) => `${x(p.i)},${y(p.r)}`).join(" ");
      el("polyline", { points: pointsAttr, class: "chart-series-line", stroke: color }, svg);
      const last = series.points[series.points.length - 1];
      el("circle", { cx: x(last.i), cy: y(last.r), r: 4, fill: color, class: "chart-series-dot" }, svg);
    });

    /* Hover layer */
    const crosshair = el("line", { y1: pad.top, y2: pad.top + plotH, class: "chart-crosshair", opacity: 0 }, svg);
    const hoverDots = data.series.map((_, index) =>
      el("circle", { r: 4.5, fill: seriesColor(index), class: "chart-hover-dot", opacity: 0 }, svg));

    const tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    root.appendChild(tooltip);

    const byIndex = data.series.map((series) => {
      const map = new Map();
      series.points.forEach((p) => map.set(p.i, p));
      return map;
    });

    let activeIndex = -1;
    const showAt = (index, clientX) => {
      activeIndex = index;
      const match = matches[index];
      crosshair.setAttribute("x1", x(index));
      crosshair.setAttribute("x2", x(index));
      crosshair.setAttribute("opacity", 1);

      tooltip.textContent = "";
      const title = document.createElement("div");
      title.className = "chart-tooltip-title";
      title.textContent = match.date;
      tooltip.appendChild(title);
      if (match.label) {
        const sub = document.createElement("div");
        sub.className = "chart-tooltip-sub";
        sub.textContent = match.label;
        tooltip.appendChild(sub);
      }
      data.series.forEach((series, seriesIndex) => {
        const point = byIndex[seriesIndex].get(index);
        const dot = hoverDots[seriesIndex];
        if (!point) { dot.setAttribute("opacity", 0); return; }
        dot.setAttribute("cx", x(index));
        dot.setAttribute("cy", y(point.r));
        dot.setAttribute("opacity", 1);
        const row = document.createElement("div");
        row.className = "chart-tooltip-row";
        const key = document.createElement("span");
        key.className = "chart-tooltip-key";
        key.style.background = seriesColor(seriesIndex);
        const name = document.createElement("span");
        name.className = "chart-tooltip-name";
        name.textContent = series.name;
        const value = document.createElement("span");
        value.className = "chart-tooltip-value";
        value.textContent = String(Math.round(point.r));
        const delta = document.createElement("span");
        delta.className = "chart-tooltip-delta " + (point.d > 0 ? "delta-up" : point.d < 0 ? "delta-down" : "delta-flat");
        delta.textContent = fmtDelta(point.d);
        row.append(key, name, value, delta);
        tooltip.appendChild(row);
      });

      tooltip.classList.add("is-visible");
      const rootRect = root.getBoundingClientRect();
      const tipW = tooltip.offsetWidth;
      const pxInRoot = clientX !== undefined
        ? clientX - rootRect.left
        : (x(index) / width) * rootRect.width;
      let left = pxInRoot + 14;
      if (left + tipW > rootRect.width - 4) left = pxInRoot - tipW - 14;
      tooltip.style.left = `${Math.max(4, left)}px`;
      tooltip.style.top = "10px";
    };

    const hide = () => {
      activeIndex = -1;
      crosshair.setAttribute("opacity", 0);
      hoverDots.forEach((dot) => dot.setAttribute("opacity", 0));
      tooltip.classList.remove("is-visible");
    };

    const indexFromEvent = (event) => {
      const rect = svg.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * width;
      const frac = (px - pad.left) / plotW;
      return Math.max(0, Math.min(totalX - 1, Math.round(frac * (totalX - 1))));
    };
    svg.addEventListener("pointermove", (event) => showAt(indexFromEvent(event), event.clientX));
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", () => showAt(totalX - 1));
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const next = activeIndex < 0 ? totalX - 1 : activeIndex + (event.key === "ArrowRight" ? 1 : -1);
        showAt(Math.max(0, Math.min(totalX - 1, next)));
      } else if (event.key === "Escape") hide();
    });

    root.appendChild(svg);

    /* Legend (always for >= 2 series; a single series is named by the card title) */
    if (data.series.length > 1) {
      const legend = document.createElement("div");
      legend.className = "chart-legend";
      data.series.forEach((series, index) => {
        const item = document.createElement("span");
        item.className = "chart-legend-item";
        const key = document.createElement("span");
        key.className = "chart-legend-key";
        key.style.background = seriesColor(index);
        const name = document.createElement("span");
        name.textContent = series.name;
        const value = document.createElement("span");
        value.className = "chart-legend-value";
        value.textContent = String(series.last);
        item.append(key, name, value);
        legend.appendChild(item);
      });
      root.appendChild(legend);
    }
  };

  /* ------------------------------------------------------------------ */
  /* Column chart (monthly activity)                                     */
  /* ------------------------------------------------------------------ */
  const renderBarChart = (root, data) => {
    root.textContent = "";
    const items = data.items || [];
    if (!items.length) return;
    const width = Math.max(root.clientWidth || 480, 300);
    const height = 210;
    const pad = { top: 18, right: 10, bottom: 26, left: 34 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const maxValue = Math.max(...items.map((item) => item.value), 1);

    const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" });

    niceTicks(0, maxValue, 3).forEach((tick) => {
      const ty = pad.top + plotH - (tick / maxValue) * plotH;
      el("line", { x1: pad.left, y1: ty, x2: pad.left + plotW, y2: ty, class: "chart-grid-line" }, svg);
      el("text", { x: pad.left - 7, y: ty + 3.5, "text-anchor": "end", class: "chart-axis-text" }, svg)
        .textContent = String(tick);
    });
    el("line", { x1: pad.left, y1: pad.top + plotH, x2: pad.left + plotW, y2: pad.top + plotH, class: "chart-axis-line" }, svg);

    const slot = plotW / items.length;
    const barW = Math.min(24, Math.max(8, slot - Math.max(2, slot * 0.4)));
    const color = cssVar("--series-1") || "#3987e5";
    const showCaps = items.length <= 8;

    const tooltip = document.createElement("div");
    tooltip.className = "chart-tooltip";
    root.appendChild(tooltip);

    items.forEach((item, index) => {
      const cx = pad.left + slot * index + slot / 2;
      const barH = Math.max(2, (item.value / maxValue) * plotH);
      const top = pad.top + plotH - barH;
      const bar = el("path", {
        d: `M ${cx - barW / 2} ${pad.top + plotH} V ${top + 4} Q ${cx - barW / 2} ${top} ${cx - barW / 2 + 4} ${top} H ${cx + barW / 2 - 4} Q ${cx + barW / 2} ${top} ${cx + barW / 2} ${top + 4} V ${pad.top + plotH} Z`,
        fill: color,
        class: "chart-bar",
      }, svg);
      if (showCaps) {
        el("text", { x: cx, y: top - 5, "text-anchor": "middle", class: "chart-axis-text" }, svg)
          .textContent = String(item.value);
      }
      el("text", { x: cx, y: height - 7, "text-anchor": "middle", class: "chart-axis-text" }, svg)
        .textContent = item.label;

      const hit = el("rect", {
        x: pad.left + slot * index, y: pad.top, width: slot, height: plotH + pad.bottom,
        fill: "transparent",
      }, svg);
      const show = () => {
        bar.style.opacity = 0.8;
        tooltip.textContent = "";
        const title = document.createElement("div");
        title.className = "chart-tooltip-title";
        title.textContent = item.full || item.label;
        tooltip.appendChild(title);
        const row = document.createElement("div");
        row.className = "chart-tooltip-row";
        const value = document.createElement("span");
        value.className = "chart-tooltip-value";
        value.textContent = String(item.value);
        row.appendChild(value);
        if (item.sub) {
          const sub = document.createElement("span");
          sub.className = "chart-tooltip-name";
          sub.textContent = item.sub;
          row.appendChild(sub);
        }
        tooltip.appendChild(row);
        tooltip.classList.add("is-visible");
        const rootRect = root.getBoundingClientRect();
        const px = (cx / width) * rootRect.width;
        let left = px + 12;
        if (left + tooltip.offsetWidth > rootRect.width - 4) left = px - tooltip.offsetWidth - 12;
        tooltip.style.left = `${Math.max(4, left)}px`;
        tooltip.style.top = "6px";
      };
      const hideBar = () => {
        bar.style.opacity = 1;
        tooltip.classList.remove("is-visible");
      };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointerleave", hideBar);
    });

    root.appendChild(svg);
  };

  /* ------------------------------------------------------------------ */
  /* Sparklines                                                          */
  /* ------------------------------------------------------------------ */
  const renderSparkline = (node) => {
    let values;
    try { values = JSON.parse(node.dataset.sparkline); } catch (e) { return; }
    if (!Array.isArray(values) || values.length < 2) return;
    const width = 100;
    const height = 28;
    const padX = 3;
    const padY = 4;
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (min === max) { min -= 1; max += 1; }
    const x = (i) => padX + (i / (values.length - 1)) * (width - padX * 2);
    const y = (v) => padY + ((max - v) / (max - min)) * (height - padY * 2);
    node.setAttribute("viewBox", `0 0 ${width} ${height}`);
    node.setAttribute("preserveAspectRatio", "none");
    node.textContent = "";
    el("polyline", { points: values.map((v, i) => `${x(i)},${y(v)}`).join(" ") }, node);
    const dot = el("circle", {
      cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 2.6, class: "spark-end",
    }, node);
    dot.setAttribute("vector-effect", "non-scaling-stroke");
  };

  /* ------------------------------------------------------------------ */
  /* Boot + re-render on resize / theme change                           */
  /* ------------------------------------------------------------------ */
  const registry = [];

  const initCharts = () => {
    document.querySelectorAll("[data-chart]").forEach((root) => {
      const script = root.querySelector("script[type='application/json']");
      if (!script) return;
      let payload;
      try { payload = JSON.parse(script.textContent); } catch (e) { return; }
      if (!payload) return;
      const kind = root.dataset.chart;
      registry.push({ root, kind, payload });
    });
    registry.forEach(render);
    document.querySelectorAll("[data-sparkline]").forEach(renderSparkline);
  };

  function render(entry) {
    if (!entry.root.isConnected) return;
    if (entry.root.clientWidth === 0 && entry.root.offsetParent === null) return;
    if (entry.kind === "line") renderLineChart(entry.root, entry.payload);
    else if (entry.kind === "bars") renderBarChart(entry.root, entry.payload);
    entry.width = entry.root.clientWidth;
  }

  let resizeTimer;
  const rerenderAll = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => registry.forEach((entry) => {
      if (entry.root.clientWidth !== entry.width || entry.root.clientWidth > 0) render(entry);
    }), 120);
  };
  window.addEventListener("resize", rerenderAll);
  window.addEventListener("elo:theme", () => registry.forEach(render));

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCharts);
  } else {
    initCharts();
  }
})();
