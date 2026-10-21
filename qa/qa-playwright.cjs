// Shared QA module resolution, including cheap legacy env aliases.
'use strict';
const explicit = process.env.QA_PLAYWRIGHT || process.env.PLAYWRIGHT_PATH || process.env.PW || process.env.PW_MODULE;
if (explicit) {
  module.exports = require(explicit);
} else {
  try { module.exports = require('playwright'); }
  catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
    try { module.exports = require('playwright-core'); }
    catch (fallback) { throw new Error('Install playwright/playwright-core or set QA_PLAYWRIGHT', { cause: fallback }); }
  }
}
