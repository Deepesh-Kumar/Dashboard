/* Dashboard JS - sorting, search, and Chart.js helpers */

function initIndexPage() {
    var searchInput = document.getElementById('search');
    var hideEmptyCheckbox = document.getElementById('hide-empty');
    var table = document.getElementById('tenants-table');
    var headers = table.querySelectorAll('th');
    var tbody = table.querySelector('tbody');

    // Search filter
    searchInput.addEventListener('input', filterRows);
    hideEmptyCheckbox.addEventListener('change', filterRows);

    function filterRows() {
        var query = searchInput.value.toLowerCase();
        var hideEmpty = hideEmptyCheckbox.checked;
        var rows = tbody.querySelectorAll('tr');
        rows.forEach(function(row) {
            var tenant = (row.getAttribute('data-tenant') || '').toLowerCase();
            var matchesSearch = !query || tenant.indexOf(query) !== -1;
            var isEmpty = row.classList.contains('row-empty');
            var matchesEmpty = !hideEmpty || !isEmpty;
            row.style.display = (matchesSearch && matchesEmpty) ? '' : 'none';
        });
    }

    // Initial filter
    filterRows();

    // Sortable columns
    headers.forEach(function(header, index) {
        header.addEventListener('click', function() {
            var sortType = header.getAttribute('data-sort');
            if (!sortType) return;

            var isAsc = header.classList.contains('sort-asc');
            headers.forEach(function(h) { h.classList.remove('sort-asc', 'sort-desc'); });
            header.classList.add(isAsc ? 'sort-desc' : 'sort-asc');

            var rows = Array.from(tbody.querySelectorAll('tr'));
            var dir = isAsc ? -1 : 1;

            rows.sort(function(a, b) {
                var aCell = a.children[index];
                var bCell = b.children[index];
                var aVal, bVal;

                if (sortType === 'number') {
                    aVal = parseFloat(aCell.getAttribute('data-value') || '0');
                    bVal = parseFloat(bCell.getAttribute('data-value') || '0');
                } else {
                    aVal = (aCell.textContent || '').toLowerCase();
                    bVal = (bCell.textContent || '').toLowerCase();
                }

                if (aVal < bVal) return -1 * dir;
                if (aVal > bVal) return 1 * dir;
                return 0;
            });

            rows.forEach(function(row) { tbody.appendChild(row); });
        });
    });
}

function initSortableTable(table) {
    if (!table) return;
    var headers = table.querySelectorAll('th');
    var tbody = table.querySelector('tbody');
    headers.forEach(function(header, index) {
        header.addEventListener('click', function() {
            var sortType = header.getAttribute('data-sort');
            if (!sortType) return;
            var isAsc = header.classList.contains('sort-asc');
            headers.forEach(function(h) { h.classList.remove('sort-asc', 'sort-desc'); });
            header.classList.add(isAsc ? 'sort-desc' : 'sort-asc');
            var rows = Array.from(tbody.querySelectorAll('tr')).filter(function(r) {
                return r.style.display !== 'none' || true; // sort all, filter handles visibility
            });
            var dir = isAsc ? -1 : 1;
            rows.sort(function(a, b) {
                var aCell = a.children[index];
                var bCell = b.children[index];
                var aVal = sortType === 'number'
                    ? parseFloat(aCell.getAttribute('data-value') || '0')
                    : (aCell.textContent || '').trim().toLowerCase();
                var bVal = sortType === 'number'
                    ? parseFloat(bCell.getAttribute('data-value') || '0')
                    : (bCell.textContent || '').trim().toLowerCase();
                if (aVal < bVal) return -1 * dir;
                if (aVal > bVal) return 1 * dir;
                return 0;
            });
            rows.forEach(function(row) { tbody.appendChild(row); });
        });
    });
}

function formatBps(value) {
    if (value >= 1e9) return (value / 1e9).toFixed(2) + ' Gbps';
    if (value >= 1e6) return (value / 1e6).toFixed(2) + ' Mbps';
    if (value >= 1e3) return (value / 1e3).toFixed(2) + ' Kbps';
    return value.toFixed(2) + ' bps';
}

function initTenantCharts(labels, rxData, txData) {
    var commonOptions = {
        responsive: true,
        plugins: {
            tooltip: {
                callbacks: {
                    label: function(ctx) {
                        return ctx.dataset.label + ': ' + formatBps(ctx.raw);
                    }
                }
            }
        },
        scales: {
            y: {
                beginAtZero: true,
                ticks: {
                    callback: function(value) { return formatBps(value); }
                }
            },
            x: {
                ticks: {
                    maxRotation: 45,
                    minRotation: 45
                }
            }
        }
    };

    // RX Chart
    var rxCtx = document.getElementById('chart-rx');
    if (rxCtx) {
        new Chart(rxCtx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Avg BW RX',
                    data: rxData,
                    backgroundColor: 'rgba(43, 108, 176, 0.7)',
                    borderColor: 'rgba(43, 108, 176, 1)',
                    borderWidth: 1
                }]
            },
            options: commonOptions
        });
    }

    // TX Chart
    var txCtx = document.getElementById('chart-tx');
    if (txCtx) {
        new Chart(txCtx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    label: 'Avg BW TX',
                    data: txData,
                    backgroundColor: 'rgba(56, 161, 105, 0.7)',
                    borderColor: 'rgba(56, 161, 105, 1)',
                    borderWidth: 1
                }]
            },
            options: commonOptions
        });
    }
}

function initCxpTimeSeriesChart(tsData) {
    // tsData: { cxp_name: { points: [[ts_ms, bps], ...], allocated_bps: N } }
    var select = document.getElementById('cxp-ts-select');
    var allocLabel = document.getElementById('cxp-ts-alloc');
    var canvas = document.getElementById('chart-cxp-ts');
    if (!select || !canvas) return;

    var cxpNames = Object.keys(tsData).sort();
    cxpNames.forEach(function(name) {
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
    });

    var currentChart = null;

    function renderChart(cxpName) {
        var entry = tsData[cxpName];
        if (!entry) return;

        var points = entry.points || [];
        var allocBps = entry.allocated_bps || 0;

        // Build labels and values
        var labels = points.map(function(p) {
            var d = new Date(p[0] * 1000);  // timestamps are in seconds
            var mo = d.getMonth() + 1;
            var dy = d.getDate();
            var hr = d.getHours();
            var mi = d.getMinutes();
            return (mo < 10 ? '0' + mo : mo) + '/' + (dy < 10 ? '0' + dy : dy) + ' ' +
                   (hr < 10 ? '0' + hr : hr) + ':' + (mi < 10 ? '0' + mi : mi);
        });
        var values = points.map(function(p) { return p[1]; });

        // Update alloc label
        if (allocBps > 0) {
            allocLabel.textContent = 'Allocated: ' + formatBps(allocBps);
        } else {
            allocLabel.textContent = '';
        }

        // Destroy old chart
        if (currentChart) {
            currentChart.destroy();
            currentChart = null;
        }

        var datasets = [{
            label: 'BW RX',
            data: values,
            borderColor: 'rgba(43, 108, 176, 1)',
            backgroundColor: 'rgba(43, 108, 176, 0.1)',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.2
        }];

        if (allocBps > 0) {
            datasets.push({
                label: 'Allocated BW',
                data: new Array(values.length).fill(allocBps),
                borderColor: 'rgba(229, 62, 62, 0.85)',
                borderWidth: 2,
                borderDash: [6, 4],
                pointRadius: 0,
                fill: false
            });
        }

        // Thin out x-axis tick labels for readability
        var tickStep = Math.max(1, Math.floor(labels.length / 30));

        currentChart = new Chart(canvas, {
            type: 'line',
            data: { labels: labels, datasets: datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                plugins: {
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        callbacks: {
                            label: function(ctx) {
                                return ctx.dataset.label + ': ' + formatBps(ctx.raw);
                            }
                        }
                    },
                    legend: { position: 'top' }
                },
                scales: {
                    x: {
                        ticks: {
                            maxRotation: 45,
                            minRotation: 45,
                            callback: function(val, idx) {
                                return idx % tickStep === 0 ? this.getLabelForValue(val) : '';
                            }
                        }
                    },
                    y: {
                        beginAtZero: true,
                        ticks: {
                            callback: function(value) { return formatBps(value); }
                        }
                    }
                }
            }
        });
    }

    // Render first CXP on load
    if (cxpNames.length > 0) {
        select.value = cxpNames[0];
        renderChart(cxpNames[0]);
    }

    select.addEventListener('change', function() {
        renderChart(this.value);
    });
}
