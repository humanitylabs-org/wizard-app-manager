for (const form of document.querySelectorAll('form[data-developer]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const result = document.querySelector('#result');
    button.disabled = true;
    try {
      const body = {action: form.dataset.developer, ...Object.fromEntries(new FormData(form))};
      if ('enabled' in body) body.enabled = body.enabled === 'true';
      if ('seconds' in body) body.seconds = Number(body.seconds);
      const response = await fetch(location.pathname, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Wizard-Store': '1'}, body: JSON.stringify(body)});
      const doc = await response.json();
      if (!response.ok || doc.error || doc.restore_release?.error) throw Error(doc.error || doc.restore_release?.error || 'Approval refused');
      // Re-read the owner page from protected state, never project form values as saved.
      location.reload();
    } catch (error) {
      result.textContent = error.message;
      button.disabled = false;
    }
  });
}
const button = document.querySelector('#approve');
if (button) button.addEventListener('click', async () => {
  button.disabled = true;
  const result = document.querySelector('#result');
  try {
    const response = await fetch(location.pathname, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Wizard-Store': '1'}, body: '{}'});
    const body = await response.json();
    if (!response.ok) throw Error(body.error || 'Approval refused');
    result.textContent = 'Approved. Retry the action in Wizard. You can close this page.';
  } catch (error) {
    result.textContent = error.message;
    button.disabled = false;
  }
});
