/* Player departures UI contract:
 * Work: pick an owned-club player in the existing Activity Centre right rail.
 * Main data: player identity, then each buyer club and its offered fee.
 * Layout: compact roster + action footer; a bounded modal for finite choices.
 * ODD: league/club typography, green actions and save-backed football prices.
 * Card choices are explicitly requested for this roguelike selection flow;
 * keep the room scene, avoid a fullscreen page, invented rarity or card glow.
 */
(() => {
  "use strict";
  function render(data, options) {
    const {t, escape:e, money, busy, error, selectedId, offersOpen} = options;
    const clubs = data?.clubs || [];
    const players = data?.players || [];
    const player = players.find(row => Number(row.id) === Number(selectedId));
    const round = player ? data?.rounds?.[String(player.id)] : null;
    const expired = Boolean(round && data?.game_date >= round.expires_date);
    const disabled = busy ? "disabled" : "";
    const clubOptions = clubs.map(club => `<option value="${Number(club.id)}" ${Number(data?.team_id) === Number(club.id) ? "selected" : ""}>${e(club.name)}</option>`).join("");
    const roster = players.map(row => {
      const selected = Number(row.id) === Number(selectedId);
      const savedRound = data?.rounds?.[String(row.id)];
      const savedValue = savedRound && !savedRound.legacy_generation && (!data?.game_date || data.game_date < savedRound.expires_date) ? savedRound.valuation : null;
      const value = savedValue || row.valuation || (Number(row.market_value) > 0 ? {amount:row.market_value,source:"market_value"} : null);
      const facts = `<span><b>${t("activity.person_position")}</b>${e((row.primary_positions || row.positions || []).join(" / ") || "—")}</span><span><b>${t("activity.person_ability")}</b>CA ${row.ca ?? "—"} / PA ${row.pa ?? "—"}</span><span><b>${t("activity.person_age")}</b>${row.age == null ? "—" : t("activity.years_value", {age:Number(row.age)})}</span>${row.squad_label ? `<span><b>${t("activity.person_team")}</b>${e(row.squad_label)}</span>` : ""}`;
      const status = [["fitness","activity.fitness"],["morale","activity.morale"],["sharpness","activity.sharpness"]].filter(([key]) => row[key] != null).map(([key,label]) => `<i>${t(label)} <strong>${Math.round(Number(row[key]))}</strong></i>`).join("");
      return `<button type="button" class="activity-player-row departure-player ${selected ? "selected" : ""}" data-departure-player="${Number(row.id)}" aria-pressed="${selected}" ${disabled}><span class="activity-player-check" aria-hidden="true">${selected ? "✓" : ""}</span><span class="activity-player-identity"><strong class="activity-person-name"><span>${e(row.name)}</span>${row.nationality ? `<span class="activity-person-nationality">${e(row.nationality)}</span>` : ""}</strong><span class="activity-person-facts">${facts}</span>${status ? `<span class="activity-person-abilities"><b>${t("activity.person_status")}</b>${status}</span>` : ""}</span><span class="departure-row-price"><small>${t(value?.source === "market_value" ? "departure.value" : "departure.estimate")}</small><strong>${value?.amount > 0 ? money(value.amount) : "—"}</strong></span></button>`;
    }).join("");
    const statusKeys = {open:"", accepted:"departure.accepted", rejected:"departure.rejected", closed:"departure.closed"};
    const offerCards = (round?.offers || []).map((offer, index) => {
      const open = offer.status === "open" && round.status === "open" && !expired;
      return `<article class="departure-offer ${offer.status === "accepted" ? "accepted" : ""} ${offer.status === "rejected" ? "rejected" : ""}"><span class="departure-choice-number" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span><header><small>${e(offer.competition_name || "—")}</small><h3>${e(offer.team_name)}</h3></header><footer><strong class="departure-price">${money(offer.amount)}</strong><div class="departure-offer-actions">${open ? `<button type="button" class="primary" data-departure-accept="${e(offer.id)}" ${busy || data?.transfer_enabled === false ? "disabled" : ""}>${t("departure.accept")}</button><button type="button" data-departure-reject="${e(offer.id)}" ${disabled}>${t("departure.reject")}</button>` : `<span>${expired && offer.status === "open" ? t("departure.error.expired") : t(statusKeys[offer.status] || "departure.closed")}</span>`}</div></footer></article>`;
    }).join("");
    const canGenerate = player && player.available !== false && !["executing", "sold"].includes(round?.status);
    const hint = round?.legacy_generation ? t("departure.expired")
      : round?.status === "executing" ? t("departure.error.uncertain")
      : round?.status === "sold" ? t("departure.done")
      : expired ? t("departure.expired")
      : player?.available === false ? t("departure.ineligible") : "";
    const valuation = round?.valuation ? `<div class="departure-valuation"><span>${t(round.valuation.source === "market_value" ? "departure.value" : "departure.estimate")}</span><strong>${money(round.valuation.amount)}</strong><time>${t("departure.expires", {date:e(round.expires_date)})}</time></div>` : "";
    const sidebar = `<footer class="departure-player-sidebar activity-roster-footer">${hint ? `<p class="departure-status" role="status">${hint}</p>` : ""}${data?.transfer_enabled === false ? `<p class="departure-status">${t("departure.error.capability")}</p>` : ""}<div class="departure-sidebar-actions">${round?.status === "sold" ? `<button type="button" data-departure-open-offers aria-haspopup="dialog" ${busy ? "disabled" : ""}>${t("departure.view_offers")}</button>` : `<button type="button" class="primary" data-departure-search aria-haspopup="dialog" ${busy || !canGenerate ? "disabled" : ""}>${t("departure.search")}</button>`}</div></footer>`;
    const offerSheet = offersOpen && round ? `<dialog class="departure-offer-dialog" data-departure-dialog aria-labelledby="departure-offer-title" aria-modal="true" aria-busy="${Boolean(busy)}"><section class="departure-offer-sheet"><header class="departure-offer-sheet-head"><div><small>${t("departure.offer_sheet")}</small><h3 id="departure-offer-title">${e(player?.name || t("departure.choose"))}</h3></div><button type="button" data-departure-close-offers autofocus ${busy ? "disabled" : ""}>${t("departure.close_offers")}</button></header>${error ? `<p class="departure-status error" role="alert">${e(error)}</p>` : ""}${busy ? `<p class="departure-status" role="status" aria-live="polite">${t("departure.generating")}</p>` : ""}${hint ? `<p class="departure-status" role="status">${hint}</p>` : ""}${valuation}<div class="departure-offers" style="--departure-choices:${Math.max(1, (round.offers || []).length)}">${offerCards || `<p class="departure-status">${t("departure.choose")}</p>`}</div></section></dialog>` : "";
    return `<section class="departure-panel" aria-busy="${Boolean(busy)}"><div class="activity-panel-head activity-roster-head departure-toolbar"><div><small>${t("departure.player_panel")}</small><h2>${t("departure.title")}</h2></div><button type="button" data-departure-back ${disabled}>${t("departure.back")}</button></div>${error && !offersOpen ? `<p class="departure-status error" role="alert">${e(error)}</p>` : ""}${busy && !offersOpen ? `<p class="departure-status" role="status" aria-live="polite">${t(data ? "departure.generating" : "departure.loading")}</p>` : ""}${!data ? "" : !clubs.length ? `<p class="empty-feature">${t("departure.no_clubs")}</p>` : `<div class="departure-roster-shell"><div class="activity-roster-toolbar departure-club-toolbar"><label>${t("departure.club")}<select data-departure-club ${disabled}>${clubOptions}</select></label><button type="button" data-departure-refresh ${disabled}>${t("departure.refresh")}</button></div><div class="activity-panel-scroll departure-roster">${roster || `<p>${t("departure.empty")}</p>`}</div>${sidebar}</div>`}${offerSheet}</section>`;
  }
  window.FMODDDepartureView = Object.freeze({render});
})();
