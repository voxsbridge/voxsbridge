(function(){
 const body=document.body, theme=document.getElementById('themeToggle'), profile=document.getElementById('profileToggle'), menu=document.getElementById('profileMenu');
 const apply=()=>{const light=localStorage.getItem('voxsbridge-theme')==='light';body.classList.toggle('light-theme',light);if(theme)theme.textContent=light?'☾':'☀'};apply();
 theme?.addEventListener('click',()=>{localStorage.setItem('voxsbridge-theme',body.classList.contains('light-theme')?'dark':'light');apply()});
 profile?.addEventListener('click',()=>{menu.hidden=!menu.hidden;profile.setAttribute('aria-expanded',String(!menu.hidden))});
 document.addEventListener('click',e=>{if(menu&&!menu.hidden&&!menu.contains(e.target)&&!profile.contains(e.target)){menu.hidden=true;profile.setAttribute('aria-expanded','false')}});
 document.getElementById('shortcutHelp')?.addEventListener('click',()=>alert('Keyboard shortcuts: Ctrl+L to focus browser address bar; Tab to navigate controls; Enter to activate buttons.'));
 document.getElementById('tourHelp')?.addEventListener('click',()=>{menu&&(menu.hidden=true);if(window.VoxsBridgeTour)window.VoxsBridgeTour.start();});
 const dnd=document.getElementById('dndToggle');if(dnd){dnd.checked=localStorage.getItem('voxsbridge-dnd')==='true';dnd.addEventListener('change',()=>{localStorage.setItem('voxsbridge-dnd',String(dnd.checked));window.voxsbridgeDnd=dnd.checked});window.voxsbridgeDnd=dnd.checked}
 const paste=document.getElementById('dialPasteButton');paste?.addEventListener('click',async()=>{const input=document.getElementById('numberInput');if(!input)return;try{const value=await navigator.clipboard.readText();input.value=value.trim();input.dispatchEvent(new Event('input',{bubbles:true}));input.focus()}catch(e){input.focus();alert('Clipboard access blocked. Press Ctrl+V in the number field.')}});
})();