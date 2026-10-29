// Approval origin only (owner_confirm.py). Confirms one server-stored intent by its digest.
const button = document.querySelector('#confirm');
if (button) button.addEventListener('click', async () => {
  button.disabled = true;
  const result = document.querySelector('#result');
  try {
    const response = await fetch('/confirm/' + encodeURIComponent(button.dataset.intent), {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Wizard-Store': '1'}, body: JSON.stringify({sha: button.dataset.sha})});
    const body = await response.json();
    if (!response.ok || body.error) throw Error(body.error || 'Confirmation refused');
    result.textContent = 'Done. You can close this page and retry in Wizard if something was waiting.';
  } catch (error) {
    result.textContent = error.message;
  }
});
