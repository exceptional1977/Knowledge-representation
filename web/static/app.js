// Масштаб колесом мыши и перетаскивание для SVG-графов (без внешних библиотек).
document.querySelectorAll('[data-zoom] svg').forEach(function (svg) {
  var vb = svg.viewBox.baseVal, start = null;
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    var k = e.deltaY < 0 ? 0.85 : 1.18, r = svg.getBoundingClientRect();
    var mx = vb.x + (e.clientX - r.left) / r.width * vb.width, my = vb.y + (e.clientY - r.top) / r.height * vb.height;
    vb.x = mx - (mx - vb.x) * k; vb.y = my - (my - vb.y) * k; vb.width *= k; vb.height *= k;
  }, {passive: false});
  svg.addEventListener('mousedown', function (e) { if (e.target.closest('a')) return; start = [e.clientX, e.clientY, vb.x, vb.y]; });
  window.addEventListener('mouseup', function () { start = null; });
  window.addEventListener('mousemove', function (e) {
    if (!start) return;
    var r = svg.getBoundingClientRect();
    vb.x = start[2] - (e.clientX - start[0]) / r.width * vb.width;
    vb.y = start[3] - (e.clientY - start[1]) / r.height * vb.height;
  });
});
