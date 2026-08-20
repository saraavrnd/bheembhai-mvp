/* BheemBhai — standardized table search (shared by every data table).
 *
 * Two wiring modes:
 *  - attachDom(inputId, tableId, emptyId): row-visibility filter on
 *    #<tableId> tbody tr. Match haystack is the row's data-search attribute
 *    when present (JS renderers stamp it with the meaningful fields so
 *    action-button labels don't match), else textContent. toggles the
 *    #<emptyId> div when a non-empty query leaves zero rows visible.
 *    JS render functions call BBTableSearch.apply(tableId) after rebuilding
 *    tbody so the filter survives re-renders.
 *  - attachQuery(inputId, cb): for tables whose whole container is rebuilt
 *    by JS — the page keeps its own query state and cb(query) re-renders
 *    data-level (same pattern as the run-state filter pills).
 *
 * Loaded in <head> of both base templates, so content-block scripts can use
 * it at parse time. Vanilla JS only — no jQuery dependency.
 */
window.BBTableSearch = (function () {
    'use strict';

    var registry = {};   // tableId -> { inputId, emptyId }

    function queryOf(input) {
        return (input && input.value ? input.value : '').toLowerCase().trim();
    }

    function rowMatches(row, q) {
        if (q === '') return true;
        var hay = row.hasAttribute('data-search') ? row.getAttribute('data-search') : row.textContent;
        return (hay || '').toLowerCase().indexOf(q) !== -1;
    }

    function filterRows(tableId) {
        var cfg = registry[tableId];
        if (!cfg) return;
        var input = document.getElementById(cfg.inputId);
        var q = queryOf(input);
        var tbody = document.querySelector('#' + tableId + ' tbody');
        if (!tbody) return;
        var shown = 0;
        Array.prototype.forEach.call(tbody.rows, function (row) {
            var hit = rowMatches(row, q);
            row.style.display = hit ? '' : 'none';
            if (hit) shown++;
        });
        if (cfg.emptyId) {
            var empty = document.getElementById(cfg.emptyId);
            if (empty) empty.style.display = (q !== '' && shown === 0) ? '' : 'none';
        }
    }

    function attachDom(inputId, tableId, emptyId) {
        registry[tableId] = { inputId: inputId, emptyId: emptyId || null };
        var input = document.getElementById(inputId);
        if (!input) return;
        input.addEventListener('input', function () { filterRows(tableId); });
        input.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') { input.value = ''; filterRows(tableId); }
        });
    }

    function attachQuery(inputId, cb) {
        var input = document.getElementById(inputId);
        if (!input) return;
        input.addEventListener('input', function () { cb(queryOf(input)); });
        input.addEventListener('keydown', function (ev) {
            if (ev.key === 'Escape') { input.value = ''; cb(''); }
        });
    }

    return {
        attachDom: attachDom,
        attachQuery: attachQuery,
        apply: filterRows,
    };
})();
