(() => {
  "use strict";

  function render({
    analysis,
    range,
    analysisLineMode,
    uiText,
    formatFullMoney,
    round,
    escapeHtml,
    formatChartAxisMoney,
  }) {
    if (!analysis) return `<div class="analysis-empty"><span class="spinner"></span>${uiText("analysis.loading")}</div>`;
    const allDaily = Array.isArray(analysis.daily) ? analysis.daily : [];
    const allTickets = Array.isArray(analysis.tickets) ? analysis.tickets : [];
    const [rangeKind, rangeValue] = String(range || "days-10").split("-");
    const lineMode = analysisLineMode === "cumulative" ? "cumulative" : "period";
    const periodLineLabel = rangeKind === "bets" ? uiText("analysis.ticket_profit") : uiText("analysis.daily_profit");
    const rangeLimit = rangeValue === "all" ? null : Number(rangeValue);
    const source = rangeKind === "bets" ? allTickets : allDaily;
    const selected = rangeLimit ? source.slice(-rangeLimit) : source;
    let selectedCumulative = 0;
    const chartRows = selected.map((row) => {
      selectedCumulative = round(selectedCumulative + Number(row.profit || 0));
      return {...row, cumulative_profit: selectedCumulative};
    });
    const rangeProfit = selectedCumulative;
    const signedMoney = (value) => `${Number(value) > 0 ? "+" : ""}${formatFullMoney(Number(value || 0))}`;
    const signedClass = (value) => Number(value) > 0 ? "profit" : Number(value) < 0 ? "loss" : "neutral";
    const lineValue = (row) => Number(lineMode === "cumulative" ? row.cumulative_profit : row.profit || 0);
    const lineLabel = lineMode === "cumulative" ? uiText("analysis.cumulative_profit") : periodLineLabel;
    const lineClass = lineMode === "cumulative" ? signedClass(rangeProfit) : "period";
    const values = chartRows.flatMap((row) => [Number(row.profit || 0), lineValue(row)]);
    const rawMaximum = Math.max(0, ...values);
    const rawMinimum = Math.min(0, ...values);
    const rawSpan = Math.max(1, rawMaximum - rawMinimum);
    const padding = rawSpan * .1;
    const maximum = rawMaximum + padding;
    const minimum = rawMinimum - padding;
    const span = maximum - minimum;
    const width = 760;
    const height = 280;
    const left = 64;
    const right = 22;
    const top = 20;
    const bottom = 44;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const y = (value) => top + (maximum - value) / span * plotHeight;
    const zeroY = y(0);
    const slotWidth = plotWidth / Math.max(1, chartRows.length);
    const xAt = (index) => left + slotWidth * (index + .5);
    const barWidth = Math.max(3, Math.min(20, slotWidth * .56));
    const yTicks = Array.from({length:5}, (_, index) => maximum - span * index / 4);
    const axisScale = Math.max(Math.abs(maximum), Math.abs(minimum));
    const grid = yTicks.map((value) => {
      const tickY = y(value);
      return `<g class="analysis-grid-row"><line x1="${left}" y1="${tickY.toFixed(2)}" x2="${width - right}" y2="${tickY.toFixed(2)}" /><text x="${left - 10}" y="${(tickY + 4).toFixed(2)}" text-anchor="end">${escapeHtml(formatChartAxisMoney(value, axisScale))}</text></g>`;
    }).join("");
    const bars = chartRows.map((row, index) => {
      const value = Number(row.profit || 0);
      const x = xAt(index) - barWidth / 2;
      const valueY = y(value);
      const unitLabel = rangeKind === "bets"
        ? uiText("analysis.ticket_unit", {number:source.length - selected.length + index + 1})
        : uiText("analysis.bet_count", {count:Number(row.bets || 0)});
      return `<rect x="${x.toFixed(2)}" y="${Math.min(zeroY, valueY).toFixed(2)}" width="${barWidth.toFixed(2)}" height="${Math.max(2, Math.abs(zeroY - valueY)).toFixed(2)}" rx="3" class="analysis-profit-bar ${value >= 0 ? "analysis-bar-profit" : "analysis-bar-loss"}"><title>${escapeHtml(uiText("analysis.bar_title", {date:row.date || "-", unit:unitLabel, period:signedMoney(value), cumulative:signedMoney(row.cumulative_profit)}))}</title></rect>`;
    }).join("");
    const linePoints = chartRows.map((row, index) => {
      return `${xAt(index).toFixed(2)},${y(lineValue(row)).toFixed(2)}`;
    }).join(" ");
    const areaPoints = `${xAt(0).toFixed(2)},${zeroY.toFixed(2)} ${linePoints} ${xAt(chartRows.length - 1).toFixed(2)},${zeroY.toFixed(2)}`;
    const markers = chartRows.map((row, index) => {
      const markerClass = lineMode === "cumulative" ? signedClass(rangeProfit) : signedClass(row.profit);
      return `<circle cx="${xAt(index).toFixed(2)}" cy="${y(lineValue(row)).toFixed(2)}" r="3" class="analysis-line-marker ${markerClass}"><title>${escapeHtml(uiText("analysis.marker_title", {date:row.date || "-", label:lineLabel, amount:signedMoney(lineValue(row))}))}</title></circle>`;
    }).join("");
    const labelIndexes = [...new Set([0, Math.floor((chartRows.length - 1) / 2), chartRows.length - 1])];
    const xLabels = labelIndexes.map((index) => `<text x="${xAt(index).toFixed(2)}" y="${height - 13}" text-anchor="middle" class="analysis-axis-label">${escapeHtml(chartRows[index]?.date?.replaceAll("-", "/") || "-")}</text>`).join("");
    const firstDate = chartRows[0]?.date?.replaceAll("-", "/") || "-";
    const lastDate = chartRows.at(-1)?.date?.replaceAll("-", "/") || "-";
    const streak = Number(analysis.current_streak || 0);
    const streakText = streak > 0 ? uiText("analysis.win_streak", {count:streak}) : streak < 0 ? uiText("analysis.loss_streak", {count:Math.abs(streak)}) : "—";
    const metric = (label, value, note = "", className = "") => `<article class="analysis-metric ${className}"><span>${label}</span><strong>${value}</strong>${note ? `<small>${note}</small>` : ""}</article>`;
    const chart = chartRows.length ? `<div class="analysis-chart-shell">
    <div class="analysis-chart-legend"><span><i class="bar"></i>${rangeKind === "bets" ? uiText("analysis.ticket_net") : uiText("analysis.daily_net")}</span><div class="analysis-line-switch" aria-label="${uiText("analysis.line_mode")}"><span>${uiText("analysis.line")}</span><button type="button" data-analysis-line="period" class="${lineMode === "period" ? "active" : ""}">${periodLineLabel}</button><button type="button" data-analysis-line="cumulative" class="${lineMode === "cumulative" ? "active" : ""}">${uiText("analysis.cumulative_profit")}</button></div><b>${uiText("analysis.date_range", {start:escapeHtml(firstDate), end:escapeHtml(lastDate)})}</b></div>
    <svg class="profit-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${uiText("analysis.chart_label", {label:lineLabel})}">
      <rect x="${left}" y="${top}" width="${plotWidth}" height="${plotHeight}" rx="5" class="analysis-plot-background" />
      ${grid}
      <line x1="${left}" y1="${zeroY.toFixed(2)}" x2="${width - right}" y2="${zeroY.toFixed(2)}" class="analysis-zero-line" />
      <polygon points="${areaPoints}" class="analysis-profit-area ${lineClass}" />
      ${bars}
      <polyline points="${linePoints}" class="analysis-profit-line ${lineClass}" />
      ${markers}
      ${xLabels}
    </svg>
  </div>` : `<div class="analysis-empty">${uiText("analysis.empty")}</div>`;
    return `<section class="bet-analysis">
    <div class="analysis-metrics">
      ${metric(uiText("analysis.net_profit"), signedMoney(analysis.net_profit), `ROI ${Number(analysis.roi || 0).toFixed(2)}%`, `featured ${signedClass(analysis.net_profit)}`)}
      ${metric(uiText("analysis.total_stake"), formatFullMoney(Number(analysis.total_stake || 0)), uiText("analysis.settled_count", {count:Number(analysis.settled_bets || 0)}))}
      ${metric(uiText("analysis.total_return"), formatFullMoney(Number(analysis.total_payout || 0)), uiText("analysis.winning_rate", {rate:Number(analysis.win_rate || 0).toFixed(2)}))}
      ${metric(uiText("analysis.pending_exposure"), formatFullMoney(Number(analysis.pending_exposure || 0)), uiText("analysis.funds_in_use"), "featured")}
      ${metric(uiText("analysis.max_profit"), signedMoney(analysis.maximum_profit), uiText("analysis.longest_wins", {count:Number(analysis.longest_win_streak || 0)}), "profit")}
      ${metric(uiText("analysis.max_loss"), signedMoney(analysis.maximum_loss), uiText("analysis.longest_losses", {count:Number(analysis.longest_loss_streak || 0)}), Number(analysis.maximum_loss) < 0 ? "loss" : "")}
    </div>
    <section class="analysis-trend-card">
      <div class="analysis-trend-head"><div><h3>${uiText("analysis.trend")}</h3><p>${uiText("analysis.explanation", {basis:rangeKind === "bets" ? uiText("analysis.node_ticket") : uiText("analysis.node_day"), period:periodLineLabel})}</p></div><div class="analysis-range-groups"><div class="analysis-range-label">${uiText("analysis.by_day")}</div><div class="analysis-range"><button data-analysis-range="days-10" class="${range === "days-10" ? "active" : ""}">${uiText("analysis.days", {count:10})}</button><button data-analysis-range="days-30" class="${range === "days-30" ? "active" : ""}">${uiText("analysis.days", {count:30})}</button><button data-analysis-range="days-all" class="${range === "days-all" ? "active" : ""}">${uiText("analysis.all")}</button></div><div class="analysis-range-label">${uiText("analysis.by_bet")}</div><div class="analysis-range"><button data-analysis-range="bets-20" class="${range === "bets-20" ? "active" : ""}">${uiText("analysis.rows", {count:20})}</button><button data-analysis-range="bets-50" class="${range === "bets-50" ? "active" : ""}">${uiText("analysis.rows", {count:50})}</button><button data-analysis-range="bets-all" class="${range === "bets-all" ? "active" : ""}">${uiText("analysis.all_bets")}</button></div></div></div>
      <div class="analysis-range-summary"><span>${uiText("analysis.range_profit", {amount:`<b class="${signedClass(rangeProfit)}">${signedMoney(rangeProfit)}</b>`})}</span><span>${uiText("analysis.current_streak", {streak:streakText})}</span><span>${uiText("analysis.record", {wins:Number(analysis.wins || 0), losses:Number(analysis.losses || 0), returns:Number(analysis.pushes || 0)})}</span></div>
      ${chart}
    </section>
  </section>`;
  }

  window.FMODDBetAnalysisPage = Object.freeze({render});
})();
