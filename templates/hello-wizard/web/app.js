'use strict';
const el = id => document.getElementById(id);
function status(text, error = false) { el('status').textContent = text; el('status').classList.toggle('error', error); }
async function request(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'The app could not save this.');
  return body;
}
function render(data) {
  el('day').textContent = 'Today · ' + data.day;
  el('empty').hidden = data.habits.length > 0;
  el('habits').replaceChildren();
  for (const habit of data.habits) {
    const item = document.createElement('li'); item.classList.toggle('done', habit.done);
    const name = document.createElement('span'); name.textContent = habit.name;
    const button = document.createElement('button'); button.type='button';
    button.textContent = habit.done ? 'Done · Undo' : 'Mark done';
    button.setAttribute('aria-pressed', String(habit.done));
    button.setAttribute('aria-label', (habit.done ? 'Undo today for ' : 'Mark today done for ') + habit.name);
    button.onclick = async () => {
      button.disabled=true;
      try { render(await request('api/habits/' + habit.id + '/today', {done: !habit.done})); status(habit.done ? 'Today undone.' : 'Today marked done.'); }
      catch (error) { status(error.message, true); }
      finally { button.disabled=false; }
    };
    item.append(name, button); el('habits').append(item);
  }
}
el('add-form').onsubmit = async event => {
  event.preventDefault(); el('add').disabled=true;
  try { render(await request('api/habits', {name:el('habit-name').value})); el('habit-name').value=''; status('Habit added.'); }
  catch (error) { status(error.message, true); }
  finally { el('add').disabled=false; }
};
// Root Back stays in this app. Home is the device-level exit action.
history.pushState({wizardAppRoot:true}, '', location.href);
addEventListener('popstate', () => history.pushState({wizardAppRoot:true}, '', location.href));
request('api/habits').then(render).catch(error => status(error.message, true));
