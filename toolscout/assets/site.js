const he = document.documentElement.lang === 'he';
const fmt = n => new Intl.NumberFormat(he ? 'he-IL' : 'en-US',{minimumFractionDigits:2,maximumFractionDigits:2}).format(n);
const val = id => Number(document.getElementById(id)?.value || 0);
document.querySelectorAll('[data-calc]').forEach(btn => btn.addEventListener('click', () => {
 const type=btn.dataset.calc, out=document.getElementById(type+'-result'); let n;
 if(type==='hourly'){if(val('hours')<=0){out.textContent=he?'יש להזין שעות חיוב חיוביות':'Enter positive billable hours';return;}n=val('income')/val('hours');out.textContent=he?fmt(n)+' ₪ לשעה':'$'+fmt(n)+' / hour';}
 if(type==='subscription'){n=val('monthly')*val('count')*12;out.textContent=he?fmt(n)+' ₪ לשנה':'$'+fmt(n)+' / year';}
 if(type==='change'){if(val('start')===0){out.textContent=he?'הערך ההתחלתי לא יכול להיות אפס':'Starting value cannot be zero';return;}n=(val('end')/val('start')-1)*100;out.textContent=(n>=0?'+':'')+n.toFixed(2)+'%';}
 if(type==='tax'){n=val('amount')*(1+val('tax')/100);out.textContent=he?fmt(n)+' ₪ סה״כ':'$'+fmt(n)+' total';}
}));