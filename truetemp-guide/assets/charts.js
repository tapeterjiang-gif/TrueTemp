/* TrueTemp usage guide — charts, diagrams and TOC behavior */
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // --- Chart: cross validation temperature RMSE ---
  var cvEl = document.getElementById('chart-cv');
  if (cvEl && window.echarts) {
    var cv = echarts.init(cvEl, null, { renderer: 'svg' });
    cv.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis',
        appendToBody: true,
        valueFormatter: function (v) { return Number(v).toFixed(4) + ' °C'; }
      },
      grid: { left: 52, right: 24, top: 34, bottom: 42 },
      xAxis: {
        type: 'category',
        data: ['全量拟合', 'CV 折 1', 'CV 折 2', 'CV 折 3'],
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 }
      },
      yAxis: {
        type: 'value',
        name: 'RMSE (°C)',
        nameTextStyle: { color: muted, fontSize: 11, padding: [0, 0, 6, 0] },
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } }
      },
      series: [{
        type: 'bar',
        barWidth: '46%',
        data: [
          { value: 0.0545, itemStyle: { color: accent } },
          { value: 0.1907, itemStyle: { color: accent2 } },
          { value: 0.9595, itemStyle: { color: accent2 } },
          { value: 0.4707, itemStyle: { color: accent2 } }
        ],
        label: {
          show: true,
          position: 'top',
          color: ink,
          fontSize: 10,
          formatter: function (p) { return Number(p.value).toFixed(3); }
        },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: accent, type: 'dashed', width: 1 },
          label: {
            formatter: '目标 0.3 °C',
            color: accent,
            fontSize: 10,
            position: 'insideEndTop'
          },
          data: [{ yAxis: 0.3 }]
        }
      }]
    });
    window.addEventListener('resize', function () { cv.resize(); });
  }

  // --- Chart: ESP32 flash footprint ---
  var flashEl = document.getElementById('chart-flash');
  if (flashEl && window.echarts) {
    var flash = echarts.init(flashEl, null, { renderer: 'svg' });
    flash.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis',
        appendToBody: true,
        valueFormatter: function (v) { return '约 ' + v + ' KiB'; }
      },
      grid: { left: 52, right: 24, top: 34, bottom: 42 },
      xAxis: {
        type: 'category',
        data: ['150 棵', '200 棵（默认）', '500 棵'],
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 }
      },
      yAxis: {
        type: 'value',
        name: 'flash (KiB)',
        nameTextStyle: { color: muted, fontSize: 11, padding: [0, 0, 6, 0] },
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } }
      },
      series: [{
        type: 'bar',
        barWidth: '46%',
        data: [
          { value: 65, itemStyle: { color: accent2 } },
          { value: 87, itemStyle: { color: accent } },
          { value: 218, itemStyle: { color: accent2 } }
        ],
        label: {
          show: true,
          position: 'top',
          color: ink,
          fontSize: 10,
          formatter: function (p) { return '≈ ' + p.value + ' KiB'; }
        }
      }]
    });
    window.addEventListener('resize', function () { flash.resize(); });
  }

  // --- TOC active state ---
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc a[href^="#"]'));
  var sections = links.map(function (link) {
    return document.querySelector(link.getAttribute('href'));
  });

  function setActive() {
    var current = 0;
    for (var i = 0; i < sections.length; i++) {
      if (sections[i] && sections[i].getBoundingClientRect().top <= 140) {
        current = i;
      }
    }
    links.forEach(function (link, i) {
      link.classList.toggle('active', i === current);
    });
  }

  var ticking = false;
  window.addEventListener('scroll', function () {
    if (!ticking) {
      window.requestAnimationFrame(function () {
        setActive();
        ticking = false;
      });
      ticking = true;
    }
  }, { passive: true });
  setActive();
})();
