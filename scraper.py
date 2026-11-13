import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
import pandas as pd
from playwright.async_api import async_playwright

SHORT_TERM_DAYS = 3
CSV_FILE = "movieland_realtime_stats.csv"
SUMMARY_FILE = "movieland_latest_summary.csv"
KST = timezone(timedelta(hours=9))

def parse_show_date(date_str, current_dt):
    match = re.search(r'(\d{1,2})\.(\d{1,2})', str(date_str))
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    year = current_dt.year + 1 if (current_dt.month == 12 and month == 1) else current_dt.year
    return datetime(year, month, day, tzinfo=current_dt.tzinfo)

async def main():
    base_url = "https://movieland.co/"
    now_dt = datetime.now(KST)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    all_records = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1080}
        )
        page = await context.new_page()

        await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)

        movie_urls = await page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a'))
                .map(a => a.href)
                .filter(href => href.startsWith('https://movieland.co/product/') && !href.includes('recent_view'));
            return [...new Set(links.map(u => u.split('?')[0]))];
        }""")

        for url in movie_urls:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)

            meta = await page.evaluate("""() => {
                const titleEl = document.querySelector('.headingArea h2, .infoArea h2, h2');
                const title = titleEl ? titleEl.innerText.trim() : document.title;
                const priceMatch = document.body.innerText.match(/([0-9,]+)원/);
                const price = priceMatch ? parseInt(priceMatch[1].replace(/,/g, '')) : 20000;
                return { title, price };
            }""")

            date_count = await page.evaluate("""() => {
                const groups = document.querySelectorAll('ul.ec-product-button');
                return groups.length >= 3 ? groups[0].querySelectorAll('li').length : 0;
            }""")

            if date_count == 0:
                continue

            for d_idx in range(date_count):
                date_info = await page.evaluate("""(idx) => {
                    const groups = document.querySelectorAll('ul.ec-product-button');
                    const li = groups[0].querySelectorAll('li')[idx];
                    if (!li) return null;
                    const isDisabled = li.className.includes('disabled') || li.className.includes('soldout');
                    const text = li.innerText.trim();
                    const clickTarget = li.querySelector('a') || li;
                    clickTarget.click();
                    return { text, isDisabled };
                }""", d_idx)

                if not date_info or date_info["isDisabled"]:
                    continue

                await page.wait_for_timeout(500)

                time_count = await page.evaluate("""() => {
                    const groups = document.querySelectorAll('ul.ec-product-button');
                    return groups[1] ? groups[1].querySelectorAll('li').length : 0;
                }""")

                for t_idx in range(time_count):
                    time_info = await page.evaluate("""(idx) => {
                        const groups = document.querySelectorAll('ul.ec-product-button');
                        const li = groups[1].querySelectorAll('li')[idx];
                        if (!li) return null;
                        const isDisabled = li.className.includes('disabled');
                        const text = li.innerText.trim();
                        if (!isDisabled) {
                            const clickTarget = li.querySelector('a') || li;
                            clickTarget.click();
                        }
                        return { text, isDisabled };
                    }""", t_idx)

                    if not time_info or time_info["isDisabled"]:
                        continue

                    await page.wait_for_timeout(500)

                    seat_result = await page.evaluate("""() => {
                        const groups = document.querySelectorAll('ul.ec-product-button');
                        if (!groups[2]) return null;
                        const seatLis = Array.from(groups[2].querySelectorAll('li'));
                        const total = seatLis.length;
                        let soldOut = 0;
                        seatLis.forEach(li => {
                            const cls = li.className || "";
                            const txt = li.innerText.trim();
                            const aTag = li.querySelector('a');
                            const titleAttr = (aTag?.getAttribute('title') || li.getAttribute('title') || "");
                            if (cls.includes('disabled') || cls.includes('soldout') || titleAttr.includes('품절') || txt.includes('품절')) {
                                soldOut += 1;
                            }
                        });
                        return { total, soldOut };
                    }""")

                    if seat_result and seat_result["total"] > 0:
                        total_seats = seat_result["total"]
                        reserved_seats = seat_result["soldOut"]
                        remain_seats = total_seats - reserved_seats
                        booking_rate = round((reserved_seats / total_seats) * 100, 1)

                        all_records.append({
                            "수집시각": now_str,
                            "영화명": meta["title"],
                            "상영일": date_info["text"],
                            "상영시간": time_info["text"],
                            "총좌석": total_seats,
                            "예매좌석": reserved_seats,
                            "잔여좌석": remain_seats,
                            "회차예매율(%)": booking_rate,
                            "추정매출(원)": reserved_seats * meta["price"]
                        })

        await browser.close()

    if not all_records:
        print("수집된 데이터가 없습니다.")
        return

    df_now = pd.DataFrame(all_records)

    if os.path.exists(CSV_FILE):
        df_now.to_csv(CSV_FILE, mode="a", header=False, index=False, encoding="utf-8-sig")
    else:
        df_now.to_csv(CSV_FILE, index=False, encoding="utf-8-sig")

    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_date = today_start + timedelta(days=SHORT_TERM_DAYS)
    df_now["parsed_date"] = df_now["상영일"].apply(lambda x: parse_show_date(x, now_dt))
    df_short = df_now[(df_now["parsed_date"] >= today_start) & (df_now["parsed_date"] <= cutoff_date)]

    total_reserved_all = df_now["예매좌석"].sum()
    full_summary = df_now.groupby("영화명").agg(
        전체회차=("상영시간", "count"),
        전체예매석=("예매좌석", "sum"),
        전체추정매출=("추정매출(원)", "sum")
    ).reset_index()
    full_summary["실시간예매율(%)"] = round((full_summary["전체예매석"] / total_reserved_all * 100), 1) if total_reserved_all > 0 else 0.0

    if not df_short.empty:
        short_summary = df_short.groupby("영화명").agg(
            단기회차=("상영시간", "count"),
            단기총좌석=("총좌석", "sum"),
            단기예매석=("예매좌석", "sum")
        ).reset_index()
        short_summary[f"단기({SHORT_TERM_DAYS}일)점유율(%)"] = round((short_summary["단기예매석"] / short_summary["단기총좌석"]) * 100, 1)
        merged = pd.merge(full_summary, short_summary, on="영화명", how="left").fillna(0)
        for col in ["단기회차", "단기총좌석", "단기예매석"]:
            merged[col] = merged[col].astype(int)
    else:
        merged = full_summary
        merged[f"단기({SHORT_TERM_DAYS}일)점유율(%)"] = 0.0

    merged.insert(0, "업데이트시각", now_str)
    merged = merged.sort_values(by="실시간예매율(%)", ascending=False)
    merged.to_csv(SUMMARY_FILE, index=False, encoding="utf-8-sig")
    print(f"[{now_str}] 수집 및 요약 저장 완료!")

if __name__ == "__main__":
    asyncio.run(main())

