(() => {
  const dataElement = document.getElementById("dashboard-chart-data");
  if (!dataElement || !window.Chart) return;

  const data = JSON.parse(dataElement.textContent);
  const root = document.documentElement;
  const palette = () => {
    const style = getComputedStyle(root);
    return {
      ink: style.getPropertyValue("--ink").trim(),
      muted: style.getPropertyValue("--muted").trim(),
      line: style.getPropertyValue("--line").trim(),
      evergreen: style.getPropertyValue("--evergreen").trim(),
      gold: style.getPropertyValue("--gold").trim(),
      navy: style.getPropertyValue("--navy").trim(),
      danger: style.getPropertyValue("--danger").trim(),
    };
  };
  const colors = palette();
  const charts = [];

  function makeChart(canvasId, type, chartData, options = {}) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    charts.push(
      new Chart(canvas, {
        type,
        data: chartData,
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: colors.ink } },
          },
          ...options,
        },
      }),
    );
  }

  makeChart(
    "church-chart",
    "bar",
    {
      labels: data.churches.labels,
      datasets: [
        {
          label: "Registrations",
          data: data.churches.values,
          backgroundColor: colors.evergreen,
          borderRadius: 5,
        },
      ],
    },
    {
      indexAxis: "y",
      scales: {
        x: { beginAtZero: true, ticks: { color: colors.muted }, grid: { color: colors.line } },
        y: { ticks: { color: colors.muted }, grid: { display: false } },
      },
    },
  );
  makeChart("status-chart", "doughnut", {
    labels: data.statuses.labels,
    datasets: [
      {
        data: data.statuses.values,
        backgroundColor: [colors.evergreen, colors.gold, colors.navy, colors.muted, colors.danger],
        borderColor: root.dataset.theme === "dark" ? colors.navy : "#fffdf8",
      },
    ],
  });
  makeChart("distribution-chart", "doughnut", {
    labels: data.distribution.labels,
    datasets: [
      {
        data: data.distribution.values,
        backgroundColor: [colors.evergreen, colors.gold],
        borderColor: root.dataset.theme === "dark" ? colors.navy : "#fffdf8",
      },
    ],
  });

  document.addEventListener("bunotan:theme-change", () => {
    const next = palette();
    charts.forEach((chart) => {
      chart.options.plugins.legend.labels.color = next.ink;
      if (chart.options.scales?.x) {
        chart.options.scales.x.ticks.color = next.muted;
        chart.options.scales.x.grid.color = next.line;
      }
      if (chart.options.scales?.y) {
        chart.options.scales.y.ticks.color = next.muted;
      }
      chart.update();
    });
  });
})();
