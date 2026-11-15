// Contador de visitas de Datalítica (GoatCounter, sin cookies ni datos personales).
// Cada visita se registra como <dominio><ruta>, para distinguir los dashboards en una sola cuenta.
window.goatcounter = { path: function (p) { return location.host + p; } };
(function () {
  var s = document.createElement('script');
  s.async = true;
  s.src = 'https://gc.zgo.at/count.js';
  s.setAttribute('data-goatcounter', 'https://datalitica.goatcounter.com/count');
  document.head.appendChild(s);
})();
