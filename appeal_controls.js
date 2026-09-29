'use strict';

// Keep native select values/events as the shared state, with an accessible menu.
function enhanceSelects() {
  let closeActive = () => {};
  for (const select of document.querySelectorAll('select')) {
    const wrapper=document.createElement('div'), button=document.createElement('button'), menu=document.createElement('div');
    wrapper.className='select-control';button.className='select-trigger';menu.className='select-menu';menu.hidden=true;
    button.type='button';button.id=select.id+'Control';menu.id=select.id+'Menu';
    const label=document.querySelector(`label[for="${select.id}"]`);
    button.setAttribute('role','combobox');button.setAttribute('aria-haspopup','listbox');
    button.setAttribute('aria-label',select.getAttribute('aria-label') || label?.textContent || select.id);
    button.setAttribute('aria-controls',menu.id);button.setAttribute('aria-expanded','false');
    menu.setAttribute('role','listbox');menu.setAttribute('aria-label',button.getAttribute('aria-label'));
    if(label) label.htmlFor=button.id;
    select.before(wrapper);wrapper.append(select,button,menu);select.hidden=true;
    let cursor=0, options=[], typeahead='', typedAt=0;
    const close=()=>{menu.hidden=true;button.setAttribute('aria-expanded','false');button.removeAttribute('aria-activedescendant');};
    const highlight=()=>{
      [...menu.children].forEach((item,i)=>item.classList.toggle('highlighted',i===cursor));
      if(menu.children[cursor]) {button.setAttribute('aria-activedescendant',menu.children[cursor].id);menu.children[cursor].scrollIntoView({block:'nearest'});}
    };
    const sync=()=>{
      options=[...select.options];button.textContent=options[select.selectedIndex]?.textContent || 'Select';
      cursor=Math.max(0,select.selectedIndex);menu.replaceChildren();
      for(const [i,option] of options.entries()) {
        const item=document.createElement('button');item.type='button';item.tabIndex=-1;
        item.id=select.id+'Option'+i;item.setAttribute('role','option');item.setAttribute('aria-selected',option.selected);
        item.textContent=option.textContent;item.disabled=option.disabled;
        item.addEventListener('click',()=>choose(i));menu.append(item);
      }
      if(!menu.hidden) highlight();
    };
    const choose=i=>{
      if(!options[i] || options[i].disabled) return;
      select.value=options[i].value;sync();close();button.focus();
      select.dispatchEvent(new Event('input',{bubbles:true}));select.dispatchEvent(new Event('change',{bubbles:true}));
    };
    const open=()=>{closeActive();sync();menu.hidden=false;button.setAttribute('aria-expanded','true');highlight();closeActive=close;};
    button.addEventListener('click',()=>menu.hidden?open():close());
    button.addEventListener('keydown',event=>{
      if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
        event.preventDefault();const wasHidden=menu.hidden;if(wasHidden) open();
        if(!wasHidden || event.key==='Home' || event.key==='End') cursor=event.key==='Home'?0:event.key==='End'?options.length-1:(cursor+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;
        highlight();
      } else if(event.key==='Enter' || event.key===' ') {
        event.preventDefault();if(menu.hidden) open();else choose(cursor);
      } else if(event.key==='Escape') {event.preventDefault();close();}
      else if(event.key==='Tab') close();
      else if(event.key.length===1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();if(menu.hidden) open();
        const now=Date.now();typeahead=(now-typedAt>600?'':typeahead)+event.key.toLowerCase();typedAt=now;
        const match=options.findIndex(option=>option.textContent.toLowerCase().startsWith(typeahead));
        if(match>=0) {cursor=match;highlight();}
      }
    });
    document.addEventListener('click',event=>{if(!wrapper.contains(event.target)) close();});
    select.addEventListener('change',sync);select.addEventListener('input',sync);
    new MutationObserver(sync).observe(select,{childList:true,subtree:true,characterData:true});
    // Scripted run selection (initial URL/back navigation) doesn't fire change.
    select.addEventListener('selection-sync',sync);
    sync();
  }
}
