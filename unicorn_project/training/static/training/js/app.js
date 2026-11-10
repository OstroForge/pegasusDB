document.addEventListener('change', async (e)=>{
if(e.target && e.target.name==='business'){
  const business=e.target.value; const locSelect=document.querySelector('select[name="training_location"]'); if(!locSelect) return;
  locSelect.innerHTML='<option>Loading...</option>';
  const res=await fetch(`/api/locations/?business=${business}`); const data=await res.json();
  locSelect.innerHTML=''; (data.data||[]).forEach(item=>{ const o=document.createElement('option'); o.value=item.id; o.textContent=item.name; locSelect.appendChild(o); });
}});

(function () {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar || typeof bootstrap === 'undefined') return;

  const mobileQuery = window.matchMedia('(max-width: 1399.98px)');

  function sidebarOffcanvas() {
    return bootstrap.Offcanvas.getOrCreateInstance(sidebar);
  }

  function hideMobileSidebar() {
    if (!mobileQuery.matches) return;
    sidebarOffcanvas().hide();
  }

  const closeBtn = sidebar.querySelector('[data-sidebar-dismiss]');
  if (closeBtn) {
    closeBtn.addEventListener('click', function () {
      sidebarOffcanvas().hide();
    });
  }

  sidebar.addEventListener('click', function (event) {
    if (event.target.closest('.nav-tree a.leaf, .sidebar-footer a')) {
      hideMobileSidebar();
    }
  });
})();
