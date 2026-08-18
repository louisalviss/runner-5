import { chromium } from 'playwright-core';
import fs from 'fs';

const requestPath = process.env.SITE_FACTORY_REQUEST;
if (!requestPath || !fs.existsSync(requestPath)) throw new Error('SITE_FACTORY_REQUEST missing');
const cfg = JSON.parse(fs.readFileSync(requestPath, 'utf8'));
const required = ['site_name','site_slug','app_name','domain_mode'];
for (const key of required) if (!cfg[key]) throw new Error(`request missing ${key}`);
if (cfg.domain_mode !== 'wasmer') throw new Error('Runner-5 bootstrap currently supports DOMAIN_MODE=wasmer only');
if (!/^[a-z0-9][a-z0-9-]{2,62}$/.test(cfg.app_name)) throw new Error('invalid app_name');

const username = process.env.WASMER_USERNAME || '';
const password = process.env.WASMER_PASSWORD || '';
const statusPath = `/tmp/site-factory-${cfg.site_slug}.json`;
const state = {
  status: 'starting', stage: 'init', siteName: cfg.site_name, siteSlug: cfg.site_slug,
  appName: cfg.app_name, domainMode: cfg.domain_mode, siteUrl: null, dashboardUrl: null,
  httpCode: null, wpAdminReachable: false, wpApiReachable: false, reusedExistingApp: false,
  detail: null, updatedAt: new Date().toISOString()
};
const save = () => { state.updatedAt = new Date().toISOString(); fs.writeFileSync(statusPath, JSON.stringify(state, null, 2)); };
const stage = (s) => { state.stage = s; save(); };
const fail = (status, detail) => { state.status = status; state.detail = detail; save(); };
const bodyText = async (page) => (await page.locator('body').innerText().catch(() => '')).replace(/\s+/g,' ').trim();
const providerBlock = async (page) => {
  const t = (await bodyText(page)).toLowerCase();
  if (/recaptcha|hcaptcha|turnstile|verify you are human|captcha/.test(t)) return 'captcha';
  if (/credit card|payment method|billing information|card details/.test(t)) return 'payment';
  if (/limit reached|quota|upgrade your plan|usage limit/.test(t)) return 'quota';
  return null;
};

save();
if (!username || !password) {
  fail('BLOCKED', 'wasmer_auth_missing');
  console.error('WASMER_USERNAME/WASMER_PASSWORD secrets are not configured in Runner-5');
  process.exit(20);
}

const owner = username;
const dashboard = (app) => `https://wasmer.io/apps/${encodeURIComponent(owner)}/${encodeURIComponent(app)}`;
const nativeUrl = (app) => `https://${app}.wasmer.app/`;

const browser = await chromium.launch({ headless: true, executablePath: '/usr/bin/google-chrome', args: ['--no-sandbox'] });
const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
const page = await ctx.newPage();

async function login() {
  stage('wasmer_login');
  await page.goto('https://wasmer.io/login', { waitUntil: 'domcontentloaded', timeout: 60000 });
  const id = page.locator('input[name=username],input[placeholder*=Username i],input[type=text]').first();
  if (!(await id.waitFor({state:'visible',timeout:12000}).then(()=>true).catch(()=>false))) throw new Error('login_identifier_missing');
  await id.fill(username);
  await id.press('Enter').catch(()=>{});
  const pass = page.locator('input[type=password]').first();
  if (!(await pass.waitFor({state:'visible',timeout:12000}).then(()=>true).catch(()=>false))) throw new Error('login_password_missing');
  await pass.fill(password);
  await pass.press('Enter').catch(()=>{});
  await page.waitForTimeout(4000);
  const block = await providerBlock(page); if (block) throw new Error(`provider_block:${block}:login`);
  if (/\/login(?:[/?#]|$)/i.test(page.url())) throw new Error('wasmer_login_failed');
}

async function appExists(app) {
  stage('check_existing_app');
  await page.goto(dashboard(app), { waitUntil:'domcontentloaded', timeout:60000 });
  for (let i=0;i<5;i++) {
    await page.waitForTimeout(900);
    const t = (await bodyText(page)).toLowerCase();
    if (page.url().includes(`/apps/${owner}/${app}`) && /wordpress|settings|domains|deployments|ready/.test(t)) return true;
    if (/page not found|404|does not exist|could not be found/.test(t)) return false;
  }
  return false;
}

async function capacityPreflight() {
  stage('capacity_preflight');
  await page.goto('https://wasmer.io/apps', { waitUntil:'domcontentloaded', timeout:60000 }).catch(()=>null);
  await page.waitForTimeout(1200);
  const block = await providerBlock(page);
  if (block === 'quota' || block === 'payment') throw new Error(`provider_block:${block}:capacity_preflight`);
}

async function createApp() {
  await capacityPreflight();
  stage('create_app');
  await page.goto('https://wasmer.io/apps/create?template=wordpress-starter', { waitUntil:'domcontentloaded', timeout:60000 });
  await page.waitForTimeout(1600);
  let block = await providerBlock(page); if (block) throw new Error(`provider_block:${block}:create_entry`);
  const inputs = page.locator('input[name*=name i],input[placeholder*=name i],input[type=text]');
  let target = null;
  for (let i=0;i<await inputs.count();i++) {
    const el = inputs.nth(i); if (!(await el.isVisible().catch(()=>false))) continue;
    const hint = `${await el.getAttribute('name')||''} ${await el.getAttribute('placeholder')||''}`;
    if (/user|email|search/i.test(hint)) continue;
    target = el; break;
  }
  if (!target) throw new Error('app_name_input_missing');
  await target.fill(cfg.app_name);
  let deploy = page.locator('button').filter({hasText:/Deploy now/i}).first();
  if (!(await deploy.count())) deploy = page.getByText(/Deploy now/i).first();
  if (!(await deploy.count())) throw new Error('deploy_button_missing');
  await deploy.click();
  for (let i=0;i<72;i++) {
    await page.waitForTimeout(2500);
    block = await providerBlock(page); if (block) throw new Error(`provider_block:${block}:after_deploy`);
    const r = await ctx.request.get(nativeUrl(cfg.app_name), {timeout:5000,failOnStatusCode:false}).catch(()=>null);
    if (r && r.status() > 0 && r.status() < 500 && r.status() !== 404) return;
  }
  throw new Error('deploy_unconfirmed');
}

async function waitReady() {
  stage('wait_wordpress_ready');
  for (let i=0;i<48;i++) {
    await page.goto(dashboard(cfg.app_name), {waitUntil:'domcontentloaded',timeout:60000}).catch(()=>null);
    await page.waitForTimeout(1000);
    const block = await providerBlock(page); if (block) throw new Error(`provider_block:${block}:dashboard`);
    if (await page.getByText(/WordPress Admin/i).first().isVisible().catch(()=>false)) return true;
    await page.waitForTimeout(1500);
  }
  return false;
}

async function verifyWordPress() {
  stage('verify_wordpress');
  state.siteUrl = nativeUrl(cfg.app_name);
  state.dashboardUrl = dashboard(cfg.app_name);
  const home = await ctx.request.get(state.siteUrl,{timeout:10000,failOnStatusCode:false}).catch(()=>null);
  state.httpCode = home?.status() ?? null;
  const api = await ctx.request.get(new URL('/wp-json/',state.siteUrl).href,{timeout:10000,failOnStatusCode:false}).catch(()=>null);
  state.wpApiReachable = !!api && api.status() >= 200 && api.status() < 400;
  await page.goto(state.dashboardUrl,{waitUntil:'domcontentloaded',timeout:60000});
  const admin = page.getByText(/WordPress Admin/i).first();
  state.wpAdminReachable = await admin.isVisible().catch(()=>false);
  save();
  if (!state.httpCode || state.httpCode >= 500 || !state.wpAdminReachable || !state.wpApiReachable) throw new Error('wordpress_readiness_failed');
}

try {
  await login();
  if (await appExists(cfg.app_name)) { state.reusedExistingApp = true; save(); }
  else await createApp();
  if (!(await waitReady())) throw new Error('app_not_ready_timeout');
  await verifyWordPress();
  state.status = 'COMPLETE'; state.stage = 'complete'; state.detail = null; save();
  console.log(JSON.stringify(state,null,2));
} catch (err) {
  const msg = String(err?.message || err);
  const blocked = /^provider_block:/.test(msg) || /captcha|payment|quota|authorization/i.test(msg);
  fail(blocked ? 'BLOCKED' : 'FAILED', msg);
  console.error(msg);
  process.exitCode = 1;
} finally {
  await browser.close().catch(()=>{});
}
