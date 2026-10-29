import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {findCatalogItem, formatLatLon, normalizeText, parseCoordinates, parsePlaces, placeLabel, searchPlaces, zoomForPopulation} from "./goto.js";

const near = (actual, expected, eps = 1e-4) => assert.ok(Math.abs(actual - expected) < eps, `${actual} != ${expected}`);
const coords = (text) => parseCoordinates(text);

test("decimal lat, lon (Google Maps order)", () => {
  assert.deepEqual(coords("24.7136, 46.6753"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("  24.7136,46.6753 "), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("24.7136 46.6753"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("24.7136; 46.6753"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("-33.8688, 151.2093"), {lat: -33.8688, lon: 151.2093});
  assert.deepEqual(coords("+21.4225, -39.8262"), {lat: 21.4225, lon: -39.8262});
  assert.deepEqual(coords("21, 39"), {lat: 21, lon: 39});
});

test("optional third value is the zoom", () => {
  assert.deepEqual(coords("24.71, 46.67, 12"), {lat: 24.71, lon: 46.67, zoom: 12});
  assert.deepEqual(coords("24.71 46.67 8.5"), {lat: 24.71, lon: 46.67, zoom: 8.5});
  assert.match(coords("24.71, 46.67, 40").error, /zoom/);
  assert.equal(coords("24.71N, 46.67E, 5N"), null);
});

test("hemisphere letters in either order and position", () => {
  assert.deepEqual(coords("24.7136N 46.6753E"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("24.7136n, 46.6753e"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("46.6753E 24.7136N"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("N 24.7136 E 46.6753"), {lat: 24.7136, lon: 46.6753});
  assert.deepEqual(coords("33.87S 151.21E"), {lat: -33.87, lon: 151.21});
  assert.deepEqual(coords("40.7N 74.0W"), {lat: 40.7, lon: -74});
  assert.match(coords("24N 46N").error, /same hemisphere/);
  assert.match(coords("-24S 46E").error, /minus/);
});

test("degrees-minutes-seconds", () => {
  const r = coords(`24°42'49"N 46°40'31"E`);
  near(r.lat, 24 + 42 / 60 + 49 / 3600); near(r.lon, 46 + 40 / 60 + 31 / 3600);
  const typographic = coords("24° 42′ 49″ N, 46° 40′ 31″ E");
  near(typographic.lat, r.lat); near(typographic.lon, r.lon);
  const minutes = coords("21°25.35'N 39°49.57'E");
  near(minutes.lat, 21 + 25.35 / 60); near(minutes.lon, 39 + 49.57 / 60);
  const west = coords(`33°51'S 151°12'W`);
  near(west.lat, -(33 + 51 / 60)); near(west.lon, -(151 + 12 / 60));
  assert.match(coords(`24°75'N 46°E`).error, /below 60/);
});

test("Arabic-Indic digits and Arabic comma", () => {
  assert.deepEqual(coords("٢٤٫٥، ٤٦"), null);   // Arabic decimal separator is not supported
  assert.deepEqual(coords("٢٤، ٤٦"), {lat: 24, lon: 46});
  assert.deepEqual(coords("۲۴.۷, ۴۶.۶"), {lat: 24.7, lon: 46.6});
});

test("first value outside +/-90 is read as lon, lat with a swap flag", () => {
  assert.deepEqual(coords("151.2093, -33.8688"), {lat: -33.8688, lon: 151.2093, swapped: true});
  assert.deepEqual(coords("46.6753, 24.7136"), {lat: 46.6753, lon: 24.7136});   // ambiguous: rule is lat first
  assert.match(coords("100, 200").error, /latitude/);
  assert.match(coords("10, 200").error, /longitude/);
});

test("non-coordinates are not parsed", () => {
  for (const text of ["", "Riyadh", "الرياض", "Route 66", "42", "1 2 3 4", "24.7, 46.7 Riyadh", "Saudi_Arabia", "24°42'.5"]) {
    assert.equal(coords(text), null, text);
  }
});

test("formatLatLon uses 5 decimals", () => {
  assert.equal(formatLatLon(24.6877, 46.72191234), "24.68770, 46.72191");
});

test("normalizeText: case, diacritics, punctuation", () => {
  assert.equal(normalizeText("  Sulţānah "), "sultanah");
  assert.equal(normalizeText("Ta’if"), "taif");
  assert.equal(normalizeText("Ta'if"), "taif");
  assert.equal(normalizeText("Saudi_Arabia"), "saudi arabia");
  assert.equal(normalizeText("São-Paulo"), "sao paulo");
  assert.equal(normalizeText("İstanbul"), "istanbul");
});

test("normalizeText: Arabic alef/hamza, ta marbuta, alef maqsura, harakat, tatweel", () => {
  assert.equal(normalizeText("أبها"), normalizeText("ابها"));
  assert.equal(normalizeText("إربد"), "اربد");
  assert.equal(normalizeText("آل"), "ال");
  assert.equal(normalizeText("جدة"), "جده");
  assert.equal(normalizeText("مكّة المُكَرَّمَة"), "مكه المكرمه");
  assert.equal(normalizeText("الريـــاض"), "الرياض");
  assert.equal(normalizeText("مصطفى"), "مصطفي");
  assert.equal(normalizeText("الطائف"), "الطايف");
  assert.equal(normalizeText("مؤتة"), "موته");
});

const SAMPLE = [
  "# header",
  "Riyadh\t\tالرياض|رياض\tSA\t24.6877\t46.7219\t4205961\tAr Riyadh|Riad",
  "Makkah\t\tمكة|مكة المكرمة\tSA\t21.4266\t39.8256\t1578722\tMecca|Mekka",
  "Jeddah\t\tجدة\tSA\t21.4901\t39.1862\t4697000\tJiddah",
  "Ta’if\tTa'if\tالطائف\tSA\t21.2703\t40.4158\t688693\t",
  "Riyadh al Khabra\t\tرياض الخبراء\tSA\t26.0\t43.5\t15000\t",
  "Medina\t\t\tUS\t41.1\t-81.8\t26000\t",
  "Madinah\t\tالمدينة المنورة\tSA\t24.4686\t39.6142\t1300000\tMedina",
  "broken line",
].join("\n");

test("parsePlaces reads the TSV and skips bad lines", () => {
  const places = parsePlaces(SAMPLE);
  assert.equal(places.length, 7);
  assert.deepEqual(places[0].labels, ["Riyadh", "الرياض", "رياض", "Ar Riyadh", "Riad"]);
  assert.equal(places[0].arabic, "الرياض");
  assert.equal(places[3].keys[0], "taif");   // ascii duplicate collapsed
  assert.equal(places[3].keys.length, 2);
});

test("searchPlaces ranks exact > prefix > substring, then population", () => {
  const places = parsePlaces(SAMPLE);
  const names = (query) => searchPlaces(places, query).map((hit) => hit.place.name);
  assert.deepEqual(names("riyadh"), ["Riyadh", "Riyadh al Khabra"]);
  assert.deepEqual(names("RIYA"), ["Riyadh", "Riyadh al Khabra"]);
  assert.deepEqual(names("الرياض"), ["Riyadh"]);
  assert.deepEqual(names("الريـاض"), ["Riyadh"]);
  assert.deepEqual(names("رياض"), ["Riyadh", "Riyadh al Khabra"]);
  assert.deepEqual(names("mecca"), ["Makkah"]);
  assert.deepEqual(names("مكه"), ["Makkah"]);
  assert.deepEqual(names("جده"), ["Jeddah"]);
  assert.deepEqual(names("taif"), ["Ta’if"]);
  assert.deepEqual(names("الطايف"), ["Ta’if"]);
  // exact on an alias beats... both exact: population decides.
  assert.deepEqual(names("medina"), ["Madinah", "Medina"]);
  assert.deepEqual(names("medina, us"), ["Medina"]);
  assert.deepEqual(names("khabra"), ["Riyadh al Khabra"]);
  assert.deepEqual(names("adh"), ["Riyadh", "Riyadh al Khabra"]);
  assert.deepEqual(names("ad"), []);   // 1-2 letters: prefix only
  assert.deepEqual(names("nowhere"), []);
  assert.deepEqual(searchPlaces(places, "a", 2).map((hit) => hit.matched), ["Ar Riyadh"]);   // alias prefix
  assert.equal(searchPlaces(places, "r", 1).length, 1);
});

test("placeLabel shows the matched name and country", () => {
  const places = parsePlaces(SAMPLE);
  const label = (query) => placeLabel(searchPlaces(places, query)[0]);
  assert.equal(label("riyadh"), "Riyadh (الرياض), SA");
  assert.equal(label("الرياض"), "الرياض (Riyadh), SA");
  assert.equal(label("mecca"), "Mecca → Makkah, SA");
  assert.equal(label("taif"), "Ta’if (الطائف), SA");
});

test("zoomForPopulation: big city ~10, town ~13", () => {
  assert.equal(zoomForPopulation(5e6), 10);
  assert.equal(zoomForPopulation(1.5e6), 10.5);
  assert.equal(zoomForPopulation(5e5), 11.5);
  assert.equal(zoomForPopulation(1.5e5), 12);
  assert.equal(zoomForPopulation(20000), 13);
  assert.equal(zoomForPopulation(undefined), 13);
});

test("findCatalogItem: exact (\"_\" == space), unique prefix, case-insensitive", () => {
  const items = [{name: "Saudi_Arabia"}, {name: "Riyadh"}, {name: "Riyadh_North", title: "Riyadh north"}, {name: "Jeddah_port"}];
  const namesOf = (item) => [item.name, item.title].filter(Boolean);
  assert.equal(findCatalogItem(items, "saudi arabia", namesOf), items[0]);
  assert.equal(findCatalogItem(items, "SAUDI_ARABIA", namesOf), items[0]);
  assert.equal(findCatalogItem(items, "riyadh", namesOf), items[1]);
  assert.equal(findCatalogItem(items, "riyadh n", namesOf), items[2]);
  assert.equal(findCatalogItem(items, "riy", namesOf), null);   // ambiguous prefix
  assert.equal(findCatalogItem(items, "jeddah", namesOf), items[3]);
  assert.equal(findCatalogItem(items, "", namesOf), null);
  assert.equal(findCatalogItem(null, "x", namesOf), null);
});

test("shipped gazetteer (public/places.tsv) finds Saudi cities in English and Arabic", () => {
  const text = readFileSync(new URL("../public/places.tsv", import.meta.url), "utf8");
  assert.ok(text.length < 2_500_000, `gazetteer is ${text.length} bytes`);
  assert.match(text.split("\n")[0], /GeoNames.*CC-BY 4\.0/);
  const places = parsePlaces(text);
  assert.ok(places.length > 30000, `${places.length} places`);
  const top = (query) => searchPlaces(places, query, 3)[0]?.place;
  for (const [query, cc, lat, lon] of [
    ["Riyadh", "SA", 24.69, 46.72], ["الرياض", "SA", 24.69, 46.72], ["الرياض، SA", "SA", 24.69, 46.72],
    ["Mecca", "SA", 21.43, 39.83], ["مكة المكرمة", "SA", 21.43, 39.83], ["مكه", "SA", 21.43, 39.83],
    ["Jeddah", "SA", 21.49, 39.19], ["جدة", "SA", 21.49, 39.19], ["Jiddah", "SA", 21.49, 39.19],
    ["Medina", "SA", 24.47, 39.61], ["المدينة المنورة", "SA", 24.47, 39.61],
    ["Dammam", "SA", 26.43, 50.10], ["الدمام", "SA", 26.43, 50.10], ["أبها", "SA", 18.22, 42.50],
    ["London", "GB", 51.51, -0.13], ["paris", "FR", 48.85, 2.35], ["القاهرة", "EG", 30.06, 31.25],
  ]) {
    const place = top(query);
    assert.ok(place, `no result for ${query}`);
    assert.equal(place.cc, cc, `${query} -> ${place.name}`);
    near(place.lat, lat, 0.1); near(place.lon, lon, 0.1);
  }
  const started = performance.now();
  for (let i = 0; i < 10; i++) searchPlaces(places, "jed");
  assert.ok((performance.now() - started) / 10 < 100, "search should take well under 100 ms");
});
