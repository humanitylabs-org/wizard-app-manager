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
