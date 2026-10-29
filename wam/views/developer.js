// Approval origin only. Each form creates an intent, then the owner confirms it on its own page.
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
      const response = await fetch('/intent', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-Wizard-Store': '1'}, body: JSON.stringify(body)});
      const doc = await response.json();
      if (!response.ok || doc.error) throw Error(doc.error || 'Refused');
      const url = new URL(doc.confirm_url);
      if (url.origin !== location.origin) throw Error('Unexpected confirmation address');
      location.assign(url.pathname);
    } catch (error) {
      result.textContent = error.message;
      button.disabled = false;
    }
  });
}
