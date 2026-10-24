(() => {
'use strict';
let leads=JSON.parse(document.getElementById('crmData').textContent);
const $=id=>document.getElementById(id), feedback=$('crmFeedback');
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statuses={new:'New',interested:'Interested',callback:'Callback',converted:'Converted',not_interested:'Not interested'};
function render(){const q=$('leadSearch').value.toLowerCase(),st=$('leadFilter').value;
$('crmTotal').textContent=leads.length;$('crmConsented').textContent=leads.filter(l=>l.consent&&!l.do_not_call).length;$('crmCallbacks').textContent=leads.filter(l=>l.status==='callback').length;$('crmDnc').textContent=leads.filter(l=>l.do_not_call).length;
const filtered=leads.filter(l=>(!st||l.status===st)&&[l.name,l.phone,l.email].some(v=>String(v||'').toLowerCase().includes(q)));
$('leadRows').innerHTML=filtered.map(l=>`<tr><td><strong>${escape(l.name)}</strong><small>${escape(l.email||'')}</small></td><td>${escape(l.phone)}</td><td><span class="module-pill">${escape(statuses[l.status]||l.status)}</span></td><td>${l.do_not_call?'Do not call':l.consent?'Yes':'No'}</td><td>${escape(l.followup_at?String(l.followup_at).replace('T',' ').slice(0,16):'—')}</td><td><button class="module-btn" data-edit="${escape(l.id)}">Edit</button> <button type="button" class="module-btn danger" data-delete="${escape(l.id)}" aria-label="Delete ${escape(l.name)}">Delete</button> <a class="module-btn" href="/dialer?number=${encodeURIComponent(l.phone)}&call=1">Call</a></td></tr>`).join('');$('leadEmpty').hidden=filtered.length>0;
}
function openLead(l){const form=$('leadForm');form.reset();for(const key of ['id','name','phone','email','status','notes'])if(l&&form.elements[key])form.elements[key].value=l[key]||'';form.elements.consent.checked=Boolean(l&&l.consent);form.elements.do_not_call.checked=Boolean(l&&l.do_not_call);form.elements.followup_at.value=l&&l.followup_at?String(l.followup_at).slice(0,16):'';$('leadDialogTitle').textContent=l?'Edit lead':'Add lead';$('leadDialog').showModal();}
$('addLeadBtn').onclick=()=>openLead(null);$('closeLead').onclick=$('cancelLead').onclick=()=>$('leadDialog').close();$('leadSearch').oninput=$('leadFilter').onchange=render;
$('leadRows').addEventListener('click',async e=>{
 const edit=e.target.closest('[data-edit]');if(edit){openLead(leads.find(l=>String(l.id)===edit.dataset.edit));return;}
 const del=e.target.closest('[data-delete]');if(!del)return;
 const lead=leads.find(l=>String(l.id)===del.dataset.delete);if(!lead||!confirm(`Delete ${lead.name} from CRM? This cannot be undone.`))return;
 del.disabled=true;
 try{const r=await fetch('/api/crm/leads/'+encodeURIComponent(lead.id),{method:'DELETE'});const body=await r.json();if(!r.ok)throw Error(body.detail||'Delete failed');leads=leads.filter(l=>String(l.id)!==String(lead.id));feedback.textContent='Lead deleted.';render();}
 catch(err){feedback.textContent=err.message;del.disabled=false;}
});
$('leadForm').onsubmit=async e=>{e.preventDefault();const f=e.target;const data={};for(const k of ['id','name','phone','email','status','notes','followup_at'])data[k]=f.elements[k].value.trim();if(!data.id)delete data.id;data.consent=f.elements.consent.checked;data.do_not_call=f.elements.do_not_call.checked;try{const r=await fetch('/api/crm/leads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const body=await r.json();if(!r.ok)throw Error(body.detail||'Save failed');const idx=leads.findIndex(l=>String(l.id)===String(body.lead.id));if(idx<0)leads.unshift(body.lead);else leads[idx]=body.lead;$('leadDialog').close();feedback.textContent='Lead saved.';render();}catch(err){feedback.textContent=err.message;}};
$('csvBtn').onclick=()=>$('csvFile').click();$('csvFile').onchange=async e=>{const file=e.target.files[0];if(!file)return;const form=new FormData();form.append('file',file);feedback.textContent='Importing...';try{const r=await fetch('/api/crm/import',{method:'POST',body:form});const body=await r.json();if(!r.ok)throw Error(body.detail||'Import failed');feedback.textContent=`Imported ${body.added} leads; skipped ${body.skipped}. Refreshing...`;location.reload();}catch(err){feedback.textContent=err.message;}finally{e.target.value='';}};
$('sampleCsv').onclick=()=>{const blob=new Blob(['name,phone,email,consent,do_not_call\nExample Lead,+12025550123,example@example.com,yes,no\n'],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='voxsbridge_crm_sample.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);};render();
})();
