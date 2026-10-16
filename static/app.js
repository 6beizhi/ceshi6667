'use strict';
function toggleMenu(button){const nav=document.getElementById('main-nav');const shown=nav.classList.toggle('open');button.setAttribute('aria-expanded',String(shown));}
document.addEventListener('DOMContentLoaded',()=>{
 const s=document.getElementById('service'),q=document.getElementById('qty');
 if(s&&q){function calc(){const price=Number(s.selectedOptions[0]?.dataset.price||0),count=Math.max(0,Number(q.value)||0),total=price*count;document.getElementById('total-preview').textContent='¥'+total.toFixed(2);document.getElementById('deposit-preview').textContent='¥'+(total*.3).toFixed(2);}s.addEventListener('change',calc);q.addEventListener('input',calc);calc();}
 document.querySelectorAll('[data-confirm]').forEach(el=>el.addEventListener('click',event=>{if(!window.confirm(el.dataset.confirm))event.preventDefault();}));
});
