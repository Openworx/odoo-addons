/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { sprintf } from "@web/core/utils/strings";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";
import {
    Component,
    onMounted,
    onPatched,
    onWillUnmount,
    useState,
    useRef,
} from "@odoo/owl";

// Chart.js, lazy-loaded via Odoo's own "web.chartjs_lib" bundle.
const EVENTS_COLOR = "#714B67"; // odoo brand purple
const BLOCKED_COLOR = "rgba(220, 53, 69, 0.75)";
const CATEGORY_PALETTE = [
    "#714B67", "#4CB5AE", "#E18A38", "#D92D4B",
    "#2E5EAA", "#875A9E", "#8F8F8F",
];
// World map is inline SVG: neutral land base plus a 6-step red heat ramp;
// the header legend gradient mirrors MAP_RAMP.
const MAP_LAND = "#e9e2ea";
const MAP_RAMP = ["#ffe3d7", "#f9b79e", "#ef8a6a", "#e05c45", "#c0392b", "#8e1b13"];
// fold length of the "also seen" list: beyond it the rest hides behind a
// visible +N control, and the unknown-origin row stays pinned.
const MAP_EXTRA_FOLD = 12;

export function foldMapExtra(rows, open) {
    if (open) {
        return rows;
    }
    const shown = [];
    const unknown = rows.find((r) => r.key === "?");
    if (unknown) {
        shown.push(unknown);
    }
    for (const row of rows) {
        if (row.key === "?") {
            continue;
        }
        if (shown.length >= MAP_EXTRA_FOLD + 1) {
            break;
        }
        shown.push(row);
    }
    return shown;
}

export class WafDashboard extends Component {
    static template = "ow_shield.Dashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            scanning: false,
            days: 1,
            data: null,
            error: null,
            mapExtraOpen: false,
        });
        this.timelineRef = useRef("wafTimeline");
        this.categoryRef = useRef("wafCategory");
        this.charts = {};
        this._libPromise = null;
        this._mapCache = null;
        this._mapExtraCache = null;
        onMounted(() => this.loadData());
        onPatched(() => this.syncCharts());
        onWillUnmount(() => {
            for (const chart of Object.values(this.charts)) {
                chart.destroy();
            }
        });
    }

    async loadData() {
        this.state.loading = true;
        this.state.error = null;
        try {
            this.state.data = await this.orm.call(
                "waf.config", "get_dashboard_data", [], { days: this.state.days });
            this._mapCache = null;
            this._mapExtraCache = null;
        } catch (error) {
            this.state.error = error.message || _t("Could not load dashboard data.");
            this.notification.add(this.state.error, { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    setDays(days) {
        if (this.state.days === days) {
            return;
        }
        this.state.days = days;
        this.loadData();
    }

    async setMode(mode) {
        const id = this.state.data.config_id;
        try {
            await this.orm.write("waf.config", [id], { mode });
            this.state.data.mode = mode;
            this.notification.add(
                _t("Firewall mode set to %s — running workers pick it up within ~5 s.",
                    mode === "enforce" ? _t("Enforce") : _t("Learning")),
                { type: "info" });
        } catch (error) {
            this.notification.add(error.message || _t("Could not change mode."),
                { type: "danger" });
        }
    }

    async runScan() {
        if (this.state.scanning) {
            return;
        }
        this.state.scanning = true;
        try {
            const action = await this.orm.call(
                "waf.config", "action_run_scan", [this.state.data.config_id]);
            await this.loadData();
            this.action.doAction(action);
        } catch (error) {
            this.notification.add(error.message || _t("The scan failed."),
                { type: "danger" });
        } finally {
            this.state.scanning = false;
        }
    }

    open(xmlid) {
        this.action.doAction(`ow_shield.${xmlid}`);
    }

    openScan() {
        const scan = this.state.data.last_scan;
        if (scan) {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "waf.scan",
                res_id: scan.id,
                views: [[false, "form"]],
            });
        } else {
            this.open("action_waf_scan");
        }
    }

    // ------------------------------------------------------------- charts

    async syncCharts() {
        const data = this.state.data;
        if (!data || !this.timelineRef.el || !this.categoryRef.el) {
            return;
        }
        if (!globalThis.Chart) {
            if (!this._libPromise) {
                this._libPromise = loadBundle("web.chartjs_lib");
            }
            try {
                await this._libPromise;
            } catch {
                return; // assets failed to load: tiles/tables still work
            }
        }
        const Chart = globalThis.Chart;
        Chart.defaults.font.size = 11;
        Chart.defaults.color = "#6c757d";

        const hourly = data.days === 1;
        const points = data.timeline || [];
        const labels = points.map(
            (p) => (hourly ? p.date.slice(11, 16) : p.date.slice(5, 10)));
        const cats = data.by_category || [];

        if (!this.charts.timeline) {
            this.charts.timeline = new Chart(this.timelineRef.el, {
                type: "line",
                data: {
                    labels,
                    datasets: [
                        {
                            type: "line",
                            // Plain strings: _t() falls back to Odoo's merged
                            // term catalog of all installed modules; do not
                            // wrap these labels in _t().
                            label: "Events",
                            data: points.map((p) => p.total),
                            borderColor: EVENTS_COLOR,
                            backgroundColor: "rgba(113, 75, 103, 0.12)",
                            borderWidth: 2,
                            tension: 0.35,
                            fill: true,
                            pointRadius: 0,
                            pointHoverRadius: 4,
                            order: 2,
                        },
                        {
                            type: "bar",
                            label: "Blocked",
                            data: points.map((p) => p.blocked),
                            backgroundColor: BLOCKED_COLOR,
                            borderRadius: 3,
                            maxBarThickness: 22,
                            order: 1,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 400 },
                    interaction: { mode: "index", intersect: false },
                    plugins: {
                        legend: {
                            labels: { usePointStyle: true, boxWidth: 7 },
                        },
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { maxTicksLimit: 12 },
                        },
                        y: {
                            beginAtZero: true,
                            ticks: { precision: 0 },
                            grid: { color: "rgba(0, 0, 0, 0.06)" },
                        },
                    },
                },
            });
        } else {
            const chart = this.charts.timeline;
            chart.data.labels = labels;
            chart.data.datasets[0].data = points.map((p) => p.total);
            chart.data.datasets[1].data = points.map((p) => p.blocked);
            chart.update();
        }

        if (!this.charts.category) {
            this.charts.category = new Chart(this.categoryRef.el, {
                type: "doughnut",
                data: {
                    labels: cats.map((c) => c.label),
                    datasets: [{
                        data: cats.map((c) => c.count),
                        backgroundColor: CATEGORY_PALETTE,
                        borderColor: "#fff",
                        borderWidth: 2,
                        hoverOffset: 6,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: "62%",
                    animation: { duration: 400 },
                    plugins: {
                        legend: {
                            position: "bottom",
                            labels: { usePointStyle: true, boxWidth: 7 },
                        },
                    },
                },
            });
        } else {
            const chart = this.charts.category;
            chart.data.labels = cats.map((c) => c.label);
            chart.data.datasets[0].data = cats.map((c) => c.count);
            chart.update();
        }
    }

    // -------------------------------------------------- world map (SVG)

    // generated, pre-projected SVG outlines: static/src/js/geo_paths.js
    get geo() {
        return globalThis.WAF_GEO ||
            { viewBox: "0 0 1000 496", land: "", countries: [] };
    }

    get mapCountries() {
        const data = this.state.data;
        if (!data) {
            return [];
        }
        if (!this._mapCache || this._mapCache.days !== data.days) {
            this._mapCache = { days: data.days, rows: this._mapRows(data) };
        }
        return this._mapCache.rows;
    }

    _mapRows(data) {
        const counts = Object.create(null);
        for (const row of data.by_country || []) {
            counts[row.code] = row;
        }
        let max = 0;
        for (const c of this.geo.countries) {
            const e = counts[c.i];
            if (e && e.total > max) {
                max = e.total;
            }
        }
        const n = MAP_RAMP.length;
        return this.geo.countries.map((c) => {
            const e = c.i ? counts[c.i] : null;
            const total = e ? e.total : 0;
            const blocked = e ? e.blocked : 0;
            // quantized into n steps so the header ramp legend stays honest
            const fill = total ? MAP_RAMP[Math.min(n - 1, Math.floor(total * n / (max + 1)))] : MAP_LAND;
            return {
                key: c.i || c.n,
                d: c.d,
                fill,
                tip: total
                    ? sprintf(_t("%(name)s: %(total)s events, %(blocked)s blocked"),
                              { name: c.n, total, blocked })
                    : c.n,
            };
        });
    }

    // Origins the base map cannot draw (ISO codes without 110m geometry,
    // '?' for hits without GeoIP attribution); listed under the map so
    // their counts stay visible.
    get mapExtraAll() {
        const data = this.state.data;
        if (!data) {
            return [];
        }
        if (!this._mapExtraCache || this._mapExtraCache.days !== data.days) {
            this._mapExtraCache = {
                days: data.days, rows: this._mapExtraRows(data),
            };
        }
        return this._mapExtraCache.rows;
    }

    get mapExtra() {
        return foldMapExtra(this.mapExtraAll, this.state.mapExtraOpen);
    }

    get mapExtraOmitted() {
        return this.mapExtraAll.length - this.mapExtra.length;
    }

    get mapExtraExpandable() {
        return this.state.mapExtraOpen || this.mapExtraOmitted > 0;
    }

    toggleMapExtra() {
        this.state.mapExtraOpen = !this.state.mapExtraOpen;
    }

    // true when a payload row has a real country code ('?' rows prove nothing mappable)
    get mapHasData() {
        const data = this.state.data;
        return !!(data && (data.by_country || []).some((c) => c.code !== "?"));
    }

    _mapExtraRows(data) {
        const drawn = new Set(
            this.geo.countries.map((c) => c.i).filter(Boolean));
        const rows = [];
        for (const row of data.by_country || []) {
            if (!drawn.has(row.code)) {
                rows.push({
                    key: row.code,
                    code: row.code === "?" ? _t("unknown origin") : row.code,
                    total: row.total,
                    blocked: row.blocked,
                });
            }
        }
        return rows; // payload is already sorted by count desc
    }

    // ISO2 code to flag emoji; "" for unknown/invalid codes so no tofu box appears
    flagEmoji(code) {
        if (typeof code !== "string" || !/^[A-Za-z]{2}$/.test(code)) {
            return "";
        }
        const A = "A".charCodeAt(0);
        const base = 0x1f1e6 - A;
        return String.fromCodePoint(
            ...code.toUpperCase().split("").map((c) => base + c.charCodeAt(0)));
    }

    gradeClass(grade) {
        if (grade === "A" || grade === "B") {
            return "text-success";
        }
        if (grade === "C" || grade === "D") {
            return "text-warning";
        }
        return "text-danger";
    }

    formatNumber(value) {
        return new Intl.NumberFormat(this.env.lang || "en-US").format(value || 0);
    }
}

registry.category("actions").add("ow_shield.dashboard", WafDashboard);
