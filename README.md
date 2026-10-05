# 🖥️ Nehru Place PC Hardware Daily Scraper & Dataset 🚀

Track PC component prices without the guesswork. This open-source tool automatically scrapes daily hardware prices from Delhi's famous Nehru Place market and consolidates them into a clean, unified CSV dataset. 

100% free, fully automated, and built for the community to navigate the market with confidence and for developers to power their own tools.

---

## ✨ Features

* 🕸️ **Blazing-Fast Daily Scraper:** A multithreaded Python crawler that pulls 17,000+ hardware listings across 15 categories in under 60 seconds.
* 📊 **Unified Market Dataset:** Consolidates CPUs, GPUs, Motherboards, RAM, Storage, Monitors, Cabinets, and peripherals into a single, easy-to-read CSV file (`data/nehru_place_prices.csv`).
* ⚡ **Zero-Maintenance Automation:** Powered by GitHub Actions to silently run every night at midnight UTC, ensuring your dataset is never a day old.
* 🛡️ **Adaptive Parsing:** Automatically handles layout quirks, missing columns, and unstructured text to ensure high data extraction quality.

## 🎯 Potential Use Cases

* **Gamers & Enthusiasts:** Check the exact real-world street prices of PC parts before you ever set foot in the market or negotiate with a dealer.
* **Developers & AI Builders:** Plug this daily-updating CSV into your own LLM agents, price-tracking dashboards, or PC-building web apps. 
* **Market Analysts & Deal Hunters:** Track historical pricing trends, spot price drops on previous-generation hardware, and monitor stock availability in India's largest IT hub.

## 🚀 Getting Started

If you just want the data, you can download the latest `data/nehru_place_prices.csv` directly from this repository—it updates automatically every day! 

If you want to run the scraper yourself locally:

### 1. Grab the Code
Clone this repository to your local machine:
```bash
git clone [https://github.com/yourusername/IndianPCBuilders.git](https://github.com/yourusername/IndianPCBuilders.git)
cd IndianPCBuilders
