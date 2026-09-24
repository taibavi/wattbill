/* Service worker: שומר את קבצי האפליקציה כדי שתיפתח גם בלי אינטרנט.
   קבצי האפליקציה – מהרשת קודם (כדי לקבל עדכונים), ומהמטמון כשאין רשת.
   בקשות לאתרים אחרים (בדיקת תעריפים) עוברות לרשת כרגיל. */
const CACHE = "wattbill-v1.5";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png",
               "./data/tariffs.json"];
/* קובץ התעריפים נשמר במטמון כמו שאר הקבצים, כדי שהאפליקציה תעבוד בלי רשת.
   אם הוא עדיין לא קיים בשרת – ההתקנה לא נכשלת בגללו. */
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c =>
    Promise.all(SHELL.map(u => c.add(u).catch(() => {})))));
  self.skipWaiting();
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request).then(r => { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r; })
      /* בלי רשת: מחזירים את העותק השמור. ניווט בלבד נופל חזרה לדף הראשי –
         בקשה לקובץ נתונים לא תקבל HTML במקום JSON. */
      .catch(() => caches.match(e.request, { ignoreSearch: true })
        .then(r => r || (e.request.mode === "navigate" ? caches.match("./index.html") : Response.error())))
  );
});
