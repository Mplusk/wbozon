// Synthetic-only browser regression. Every network request is intercepted.
// No mailbox, user credentials, production domain, remote API or server required.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try {
  for(const [policy,target,expectedOrigin,expectedReferrer] of [
    ['no-referrer','https://portal.invalid/submit','null',undefined],
    ['strict-origin','https://portal.invalid/submit','https://portal.invalid','https://portal.invalid/'],
    ['strict-origin','https://foreign.invalid/submit','https://portal.invalid','https://portal.invalid/']
  ]) {
   const context=await browser.newContext();const page=await context.newPage();let seen;
   await context.route('**/*',async route=>{
    const req=route.request();
    if(req.method()==='POST') {
     seen=await req.allHeaders();
     await route.fulfill({status:200,contentType:'text/plain',body:'synthetic complete'});
    } else {
     await route.fulfill({status:200,contentType:'text/html',headers:{'Referrer-Policy':policy},
      body:`<!doctype html><form method="post" action="${target}"><input name="value" value="synthetic"><button>Submit</button></form>`});
    }
   });
   await page.goto('https://portal.invalid/form?token=SYNTHETIC-NOT-A-SECRET');
   await Promise.all([page.waitForURL(target),page.locator('button').click()]);
   assert.ok(seen,'POST must occur');assert.equal(seen.origin,expectedOrigin);assert.equal(seen.referer,expectedReferrer);
   assert.ok(!(seen.referer||'').includes('token='));
   console.log(JSON.stringify({policy,target_origin:new URL(target).origin,origin:seen.origin,referer:seen.referer??null}));
   await context.close();
  }
 } finally {await browser.close();}
})().catch(e=>{console.error(e.message);process.exitCode=1});
