import { useEffect, useRef, useState } from 'react';
import { createChart, ColorType, IChartApi, ISeriesApi, LineSeries } from 'lightweight-charts';
import { Activity, TrendingUp, TrendingDown, Clock, Wallet, Settings2, Check, Link } from 'lucide-react';

interface MarketData {
  current_price: number;
  price_to_beat: number;
  time_left: number;
  cycle_duration: number;
  volatility?: number;
  up_price: number;
  down_price: number;
  warmup_seconds_left?: number;
}

interface PortfolioData {
  balance: number;
  positions: { up: number; down: number };
  spent: { up: number; down: number };
  total_spent: number;
  live_profit: number;
  live_profit_up: number;
  live_profit_down: number;
  live_value_up: number;
  live_value_down: number;
  live_value: number;
  history: Array<{
    time: string;
    winning_side: string;
    up_shares: number;
    down_shares: number;
    spent: number;
    payout: number;
    profit: number;
  }>;
}

interface StrategiesData {
  strategy_a: boolean;
  strategy_b: boolean;
  strategy_c: boolean;
  strategy_c_trailing: boolean;
  strategy_d: boolean;
  strategy_e: boolean;
  strategy_f: boolean;
  strategy_7: boolean;
  strategy_cpt: boolean;
  strategy_claude: boolean;
  claude_confidence: number;
  claude_phase: number;
  claude_exit_reason: string;
  profit_target: number;
  strikes: number;
  b_done: boolean;
  live_mode: boolean;
  live_available: boolean;
}

function App() {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  const [market, setMarket] = useState<MarketData>({
    current_price: 0, price_to_beat: 0, time_left: 0, cycle_duration: 3600, volatility: 0.010, up_price: 50, down_price: 50
  });
  const [portfolio, setPortfolio] = useState<PortfolioData>({
    balance: 10000.0, positions: { up: 0, down: 0 }, spent: { up: 0, down: 0 }, total_spent: 0, live_profit: 0, live_profit_up: 0, live_profit_down: 0, live_value_up: 0, live_value_down: 0, live_value: 0, history: []
  });
  const [strategies, setStrategies] = useState<StrategiesData>({
    strategy_a: false, strategy_b: false, strategy_c: false, strategy_c_trailing: false, strategy_d: false, strategy_e: false, strategy_f: false, strategy_7: false, strategy_cpt: false, strategy_claude: false, claude_confidence: 0, claude_phase: 1, claude_exit_reason: '', profit_target: 4.0, strikes: 0, b_done: false, live_mode: false, live_available: false
  });
  const [strategyStats, setStrategyStats] = useState<Record<string, {trades:number;wins:number;total_profit:number;best:number;worst:number}>>({});
  const [ws, setWs] = useState<WebSocket | null>(null);
  const [liveSlugInputValue, setLiveSlugInputValue] = useState("");
  const [activeLiveSlug, setActiveLiveSlug] = useState("");
  const [isSyncing, setIsSyncing] = useState(false);
  const [isAutoPilotEnabled, setIsAutoPilotEnabled] = useState(true);
  const manualTargetRef = useRef<number | null>(null);
  const [isTargetEditing, setIsTargetEditing] = useState(false);
  const lastAutoTargetHour = useRef<number>(-1);

  // On page load, immediately connect to the current hourly market
  useEffect(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN || activeLiveSlug) return;
    const connectCurrentMarket = async () => {
      try {
        const now = new Date();
        const options: Intl.DateTimeFormatOptions = { timeZone: 'America/New_York' };
        const month = now.toLocaleString('en-US', { month: 'long', ...options }).toLowerCase();
        const day = now.toLocaleString('en-US', { day: 'numeric', ...options });
        const hour24 = parseInt(now.toLocaleString('en-US', { hour: 'numeric', hourCycle: 'h23', ...options }));
        const ampm = hour24 >= 12 ? 'pm' : 'am';
        const hour12 = hour24 % 12 || 12;
        const currentSlug = `bitcoin-up-or-down-${month}-${day}-${hour12}${ampm}-et`;
        const res = await fetch(`http://${window.location.hostname}:8000/proxy/gamma/?slug=${currentSlug}&cb=${Date.now()}`);
        const data = await res.json();
        if (data && data.length > 0 && !data[0].closed) {
          setLiveSlugInputValue(currentSlug);
          ws.send(JSON.stringify({ action: "CONNECT_GAMMA", slug: currentSlug }));
          setActiveLiveSlug(currentSlug);
        }
      } catch (e) {}
    };
    connectCurrentMarket();
  }, [ws, activeLiveSlug]);

  // Auto-fetch BTC hourly candle open price from Binance
  useEffect(() => {
    const fetchBtcOpenPrice = async () => {
      const currentHour = new Date().getUTCHours();
      if (lastAutoTargetHour.current === currentHour && manualTargetRef.current !== null) return;
      try {
        const res = await fetch("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1");
        const data = await res.json();
        if (data && data.length > 0) {
          const openPrice = parseFloat(data[0][1]);
          manualTargetRef.current = openPrice;
          lastAutoTargetHour.current = currentHour;
          setMarket(prev => ({ ...prev, price_to_beat: openPrice }));
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "SYNC_LIVE_GAMMA", target_price: openPrice }));
          }
        }
      } catch (e) {
        console.error("Binance open price fetch failed", e);
      }
    };
    fetchBtcOpenPrice();
    const interval = setInterval(fetchBtcOpenPrice, 60000);
    return () => clearInterval(interval);
  }, [ws]);

  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;
    if (isAutoPilotEnabled && ws?.readyState === WebSocket.OPEN) {
      interval = setInterval(async () => {
        // Run auto-pilot if market is expired or less than 5s left
        // AND we haven't already synced successfully
        if (market.time_left <= 5) {
          console.log("Auto-Pilot detected market expired. Locating next cycle mathematically...");
          try {
            const nowSeconds = Math.floor(Date.now() / 1000);
            let targetSlug = "";

            if (market.cycle_duration === 300) {
              // 5-minute markets snap to exact 300s UTC boundaries
              let nextBoundary = Math.ceil(nowSeconds / 300) * 300;
              if (nextBoundary - nowSeconds < 60) nextBoundary += 300;
              targetSlug = `btc-updown-5m-${nextBoundary}`;
            } else if (market.cycle_duration === 900) {
              // 15-minute markets snap to exact 900s UTC boundaries
              let nextBoundary = Math.ceil(nowSeconds / 900) * 900;
              if (nextBoundary - nowSeconds < 60) nextBoundary += 900;
              targetSlug = `btc-updown-15m-${nextBoundary}`;
            } else if (market.cycle_duration === 3600) {
              // Hourly markets use human-readable ET strings like 'bitcoin-up-or-down-march-8-6pm-et'
              const safeTime = new Date((nowSeconds + 300) * 1000);
              safeTime.setMinutes(0, 0, 0);
              safeTime.setHours(safeTime.getHours() + 1);

              const options: Intl.DateTimeFormatOptions = { timeZone: 'America/New_York' };
              const monthFormatter = new Intl.DateTimeFormat('en-US', { month: 'long', ...options });
              const month = monthFormatter.format(safeTime).toLowerCase();
              const day = safeTime.toLocaleString('en-US', { day: 'numeric', ...options });
              const hour24 = parseInt(safeTime.toLocaleString('en-US', { hour: 'numeric', hourCycle: 'h23', ...options }));

              const ampm = hour24 >= 12 ? 'pm' : 'am';
              const hour12 = hour24 % 12 || 12;
              targetSlug = `bitcoin-up-or-down-${month}-${day}-${hour12}${ampm}-et`;
            }

            if (!targetSlug) return;
            // Prevent getting stuck in a fast loop if the target slug is exactly what we are already on
            if (targetSlug === activeLiveSlug) {
              // The current active slug is the future one, wait for real time to pass it.
              return;
            }

            // Verify if Polymarket actually generated this market yet
            const res = await fetch(`http://${window.location.hostname}:8000/proxy/gamma/?slug=${targetSlug}&cb=${Date.now()}`);
            const data = await res.json();

            // If it exists in their system, instantly leap to it
            if (data && data.length > 0 && !data[0].closed) {
              console.log("Auto-Pilot syncing next mathematically predicted market:", targetSlug);
              setIsSyncing(true);
              setLiveSlugInputValue(targetSlug);
              ws.send(JSON.stringify({ action: "CONNECT_GAMMA", slug: targetSlug }));
              setActiveLiveSlug(targetSlug);
              setTimeout(() => setIsSyncing(false), 1500);
            } else {
              console.log("Mathematically predicted market not ready yet on Polymarket. Retrying in 15 seconds...");
            }
          } catch (err) {
            console.error("AutoPilot computation failed", err);
          }
        }
      }, 15000); // Check every 15 seconds
    }
    return () => clearInterval(interval);
  }, [isAutoPilotEnabled, ws, market.time_left, activeLiveSlug]);

  useEffect(() => {
    console.log("Gamma Hook Evaluated:", { activeLiveSlug, hasWs: !!ws, readyState: ws?.readyState });
    if (!activeLiveSlug || !ws || ws.readyState !== WebSocket.OPEN) {
      console.log("Gamma Hook Bailed Out Early!");
      return;
    }

    const pollGamma = async () => {
      try {
        console.log("pollGamma executing fetch!");
        let slug = activeLiveSlug;
        if (slug.includes("polymarket.com/event/")) {
          slug = slug.split("polymarket.com/event/")[1].split("/")[0].split("?")[0];
        }

        const targetUrl = `http://${window.location.hostname}:8000/proxy/gamma/?slug=${slug}&cb=${Date.now()}`;
        const res = await fetch(targetUrl);

        let serverTimeOffset = 0;
        const serverDateStr = res.headers.get("date");
        if (serverDateStr && serverDateStr !== "") {
          serverTimeOffset = new Date(serverDateStr).getTime() - Date.now();
        }

        const data = await res.json();

        if (data && data.length > 0) {
          const event = data[0];
          const markets = event.markets || [];
          for (const m of markets) {
            if (!m.closed && m.active) {
              const outcomes = JSON.parse(m.outcomes || "[]");

              const yesIdx = outcomes.findIndex((o: string) => {
                const lower = o.toLowerCase();
                return lower === "yes" || lower === "up" || lower === "above";
              });
              const noIdx = outcomes.findIndex((o: string) => {
                const lower = o.toLowerCase();
                return lower === "no" || lower === "down" || lower === "below";
              });

              if (yesIdx !== -1 && noIdx !== -1) {
                let upPrice = 50;
                let downPrice = 50;

                try {
                  const prices = JSON.parse(m.outcomePrices || "[]");
                  if (prices.length > 0) {
                    upPrice = Math.round(parseFloat(prices[yesIdx]) * 100);
                    downPrice = Math.round(parseFloat(prices[noIdx]) * 100);
                  }
                } catch (e) {
                  console.error("Failed to parse outcome prices", e);
                }

                if (m.clobTokenIds) {
                  try {
                    const tokens = JSON.parse(m.clobTokenIds || "[]");
                    if (tokens.length > 1) {
                      const upToken = tokens[yesIdx];
                      const downToken = tokens[noIdx];

                      const fetchClobPrice = async (t: string) => {
                        try {
                          const controller = new AbortController();
                          const timeoutId = setTimeout(() => controller.abort(), 6000); // 6s to account for proxy overhead
                          // const targetUrl = encodeURIComponent(`https://clob.polymarket.com/book?token_id=${t}`);
                          const res = await fetch(`http://${window.location.hostname}:8000/proxy/clob/${t}`, { signal: controller.signal });
                          clearTimeout(timeoutId);
                          if (res.ok) {
                            const data = await res.json();
                            if (data.asks && data.asks.length > 0) {
                              const minAsk = Math.min(...data.asks.map((a: any) => parseFloat(a.price)));
                              return Math.round(minAsk * 100);
                            }
                          }
                        } catch (e) {
                          console.log("Clob fetch error", e);
                        }
                        return null;
                      };

                      const [upLive, downLive] = await Promise.all([fetchClobPrice(upToken), fetchClobPrice(downToken)]);

                      if (upLive !== null) upPrice = upLive;
                      if (downLive !== null) downPrice = downLive;
                    }
                  } catch (e) {
                    console.error("CLOB proxy lookup collapsed", e);
                  }
                }

                upPrice = Math.max(1, Math.min(99, upPrice));
                downPrice = Math.max(1, Math.min(99, downPrice));

                const targetPrice = manualTargetRef.current || 0;

                let timeRemainingSeconds = 0;
                const syncedNow = Date.now() + serverTimeOffset;
                const titleLower = (event.title || "").toLowerCase();
                const slugLower = activeLiveSlug.toLowerCase();
                
                if (titleLower.includes("- hourly") || titleLower.includes("up or down") || slugLower.includes("hourly") || slugLower.includes("-et")) {
                  const nowSecs = Math.floor(syncedNow / 1000);
                  const nextHour = Math.ceil(nowSecs / 3600) * 3600;
                  timeRemainingSeconds = nextHour - nowSecs;
                } else if (slugLower.includes("15m") || titleLower.includes("15m")) {
                  const nowSecs = Math.floor(syncedNow / 1000);
                  const next15m = Math.ceil(nowSecs / 900) * 900;
                  timeRemainingSeconds = next15m - nowSecs;
                } else if (slugLower.includes("5m") || titleLower.includes("5m")) {
                  const nowSecs = Math.floor(syncedNow / 1000);
                  const next5m = Math.ceil(nowSecs / 300) * 300;
                  timeRemainingSeconds = next5m - nowSecs;
                } else {
                  const endDateStr = m.endDate || event.endDate;
                  if (endDateStr) {
                    timeRemainingSeconds = Math.max(0, (new Date(endDateStr).getTime() - syncedNow) / 1000);
                  }
                }

                ws.send(JSON.stringify({
                  action: "SYNC_LIVE_GAMMA",
                  up_price: upPrice,
                  down_price: downPrice,
                  title: event.title,
                  target_price: targetPrice,
                  time_remaining: timeRemainingSeconds
                }));
                break;
              }
            }
          }
        }
      } catch (e) {
        console.error("Gamma fetch error:", e);
      }
    };

    pollGamma();
    const interval = setInterval(pollGamma, 4000);
    return () => clearInterval(interval);
  }, [activeLiveSlug]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const wsHost = window.location.hostname + ':8000';
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(`${wsProtocol}//${wsHost}/ws`);
      setWs(socket);

      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "error") {
            alert(`⚠️ ${msg.message}`);
            return;
          }
          if (msg.type === "state_update") {
            setMarket(msg.data.market);
            setPortfolio(msg.data.portfolio);
            setStrategies(msg.data.strategies);
            if (msg.data.strategy_stats) setStrategyStats(msg.data.strategy_stats);

            if (msg.data.market?.is_live && msg.data.market?.live_slug) {
                setActiveLiveSlug((prev) => prev === "" ? msg.data.market.live_slug : prev);
                setLiveSlugInputValue((prev) => prev === "" ? msg.data.market.live_slug : prev);
            }

            const now = Math.floor(Date.now() / 1000);
            const price = msg.data.market.current_price;
            if (price > 0 && seriesRef.current) {
              seriesRef.current.update({ time: now as any, value: price });
            }
          }
        } catch (e) {
          console.error("WS Message Error:", e);
        }
      };

      socket.onclose = () => {
        console.log("WebSocket disconnected. Reconnecting in 2s...");
        reconnectTimer = setTimeout(connect, 2000);
      };

      socket.onerror = (err) => {
        console.error("WebSocket error:", err);
        socket?.close();
      };
    };

    connect();

    return () => {
      clearTimeout(reconnectTimer);
      if (socket) {
        socket.onclose = null; // Prevent reconnect on unmount
        socket.close();
      }
    };
  }, []);

  useEffect(() => {
    if (chartContainerRef.current) {
      const handleResize = () => {
        if (chartRef.current && chartContainerRef.current) {
          chartRef.current.applyOptions({ width: chartContainerRef.current.clientWidth });
        }
      };

      chartRef.current = createChart(chartContainerRef.current, {
        layout: {
          background: { type: ColorType.Solid, color: '#111827' },
          textColor: '#9CA3AF',
        },
        grid: {
          vertLines: { color: '#1F2937' },
          horzLines: { color: '#1F2937' },
        },
        width: chartContainerRef.current.clientWidth,
        height: 400,
        timeScale: {
          timeVisible: true,
          secondsVisible: true,
        }
      });

      seriesRef.current = chartRef.current.addSeries(LineSeries, {
        color: '#3B82F6',
        lineWidth: 2,
        crosshairMarkerVisible: true,
      });

      window.addEventListener('resize', handleResize);
      return () => {
        window.removeEventListener('resize', handleResize);
        if (chartRef.current) chartRef.current.remove();
      };
    }
  }, []);

  const formatTime = (seconds: number) => {
    const h = Math.floor(seconds / 3600).toString().padStart(2, '0');
    const m = Math.floor((seconds % 3600) / 60).toString().padStart(2, '0');
    const s = Math.floor(seconds % 60).toString().padStart(2, '0');
    if (h === '00') {
      return `${m}:${s}`;
    }
    return `${h}:${m}:${s}`;
  };

  const buyUp = () => {
    if (ws) ws.send(JSON.stringify({ action: "BUY_UP", shares: 10 })); // Buying 10 shares manually
  };

  const buyDown = () => {
    if (ws) ws.send(JSON.stringify({ action: "BUY_DOWN", shares: 10 }));
  };

  const toggleStrategy = (strategy: 'A' | 'B' | 'C' | 'C_TRAILING' | 'D' | 'E' | 'F' | '7' | 'CPT' | 'CLAUDE') => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: `TOGGLE_STRATEGY_${strategy}` }));
    }
  };

  const setProfitTarget = (val: number) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: "SET_PROFIT_TARGET", value: val }));
    }
  };

  const cashOut = () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: "CASH_OUT" }));
    }
  };

  const changeTimeframe = (seconds: number) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: "CHANGE_TIMEFRAME", seconds }));
      // Optimistically update the UI to feel more responsive
      setMarket(prev => ({ ...prev, cycle_duration: seconds, time_left: seconds }));
    }
  };

  const changeVolatility = (value: number) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: "SET_VOLATILITY", value }));
      setMarket(prev => ({ ...prev, volatility: value }));
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 font-sans selection:bg-blue-500/30 p-6">
      <header className="flex justify-between items-center mb-8 bg-gray-900 border border-gray-800 p-4 rounded-2xl shadow-xl">
        <div className="flex items-center space-x-3">
          <Activity className="text-blue-500 w-8 h-8" />
          <h1 className="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-indigo-400 ml-3 mr-6">
            Quantum Trade
          </h1>
          <div className="flex items-center space-x-1 bg-gray-900 border border-gray-700/50 p-1 rounded-xl shadow-inner">
            {[
              { label: '5M', value: 300 },
              { label: '15M', value: 900 },
              { label: '1H', value: 3600 }
            ].map(tf => (
              <button
                key={tf.value}
                onClick={() => changeTimeframe(tf.value)}
                className={`px-4 py-1.5 rounded-lg text-sm font-bold transition-all duration-300 ${market.cycle_duration === tf.value
                  ? 'bg-blue-600 text-white shadow-md shadow-blue-500/20'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
                  }`}
              >
                {tf.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center space-x-4">
          {/* Paper / Live Toggle */}
          <button
            disabled={!strategies.live_available}
            onClick={() => {
              if (!strategies.live_available) return;
              if (!strategies.live_mode) {
                const confirmed = confirm('⚠️ ENABLE LIVE TRADING?\n\nThis will execute REAL trades on Polymarket with REAL money.\n\nAre you absolutely sure?');
                if (!confirmed) return;
              }
              if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ action: 'TOGGLE_LIVE_MODE' }));
              }
            }}
            className={`flex items-center space-x-2 px-4 py-2 rounded-xl font-bold text-sm border transition-all duration-300 shadow-lg ${strategies.live_mode
                ? 'bg-red-600/20 border-red-500/50 text-red-400 shadow-red-500/20 animate-pulse'
                : 'bg-emerald-600/20 border-emerald-500/50 text-emerald-400 shadow-emerald-500/10'
              }`}
          >
            <span className={`w-2.5 h-2.5 rounded-full ${strategies.live_mode ? 'bg-red-500' : 'bg-emerald-500'}`} />
            <span>{strategies.live_mode ? '🔴 LIVE' : '📝 PAPER'}</span>
          </button>

          <div className="flex flex-col items-end">
            <span className="text-xs text-gray-400 font-semibold uppercase tracking-wider mb-1">Portfolio Balance</span>
            <div className="flex items-center space-x-2 text-xl font-bold text-emerald-400 bg-emerald-950/30 px-4 py-1.5 rounded-xl border border-emerald-900/50">
              <Wallet className="w-5 h-5 text-emerald-500" />
              <span>${portfolio.balance.toFixed(2)}</span>
            </div>
          </div>
        </div>
      </header>

      <div className="flex items-center bg-gray-900 border border-blue-900/50 p-3 rounded-2xl shadow-lg mb-6 ring-1 ring-blue-500/20">
        <Link className="w-5 h-5 text-blue-400 ml-2" />
        <input
          type="text"
          placeholder="Paste Polymarket Market URL or Slug..."
          value={liveSlugInputValue}
          onChange={(e) => setLiveSlugInputValue(e.target.value)}
          className="flex-grow bg-transparent border-none text-white text-sm px-4 focus:outline-none placeholder-gray-600"
        />
        <button
          onClick={() => {
            console.log("Sync Button Clicked! ws readyState:", ws?.readyState);
            if (ws && liveSlugInputValue.trim()) {
              setIsSyncing(true);
              try {
                if (ws.readyState === WebSocket.OPEN) {
                  ws.send(JSON.stringify({ action: "CONNECT_GAMMA", slug: liveSlugInputValue.trim() }));
                } else {
                  console.warn("WebSocket not fully open. ReadyState:", ws.readyState);
                }
                setActiveLiveSlug(liveSlugInputValue.trim());
              } catch (err) {
                console.error("Failed to push sync data via ws", err);
              } finally {
                setTimeout(() => { setIsSyncing(false); }, 1500);
              }
            } else {
              console.log("Cannot sync. ws:", ws, "slug:", liveSlugInputValue);
            }
          }}
          className="px-6 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-bold rounded-xl shadow-lg shadow-blue-500/20 transition-all duration-300"
          id="sync-btn"
          disabled={isSyncing}
        >
          {isSyncing ? "Syncing..." : "Sync Live API"}
        </button>

        <button
          onClick={() => setIsAutoPilotEnabled(!isAutoPilotEnabled)}
          className={`px-4 py-2 ml-4 text-sm font-bold rounded-xl shadow-lg transition-all duration-300 flex items-center space-x-2 border
            ${isAutoPilotEnabled
              ? 'bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 border-emerald-500/50 shadow-emerald-500/20'
              : 'bg-gray-800 hover:bg-gray-700 text-gray-400 border-gray-700'
            }`}
        >
          {isAutoPilotEnabled ? (
            <><Check className="w-4 h-4" /> <span>24/7 Auto-Pilot: ON</span></>
          ) : (
            <span>Auto-Pilot: OFF</span>
          )}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Left Column - Market Data & Chart */}
        <div className="lg:col-span-2 space-y-6">

          <div className="grid grid-cols-3 gap-6">
            <div className="bg-gray-900 border border-gray-800 p-6 rounded-2xl shadow-lg relative overflow-hidden group">
              <div className="absolute top-0 left-0 w-1 h-full bg-blue-500"></div>
              <span className="text-sm text-gray-400 font-medium mb-2 block">Polymarket Prices</span>
              <div className="flex items-baseline gap-3">
                <div>
                  <span className="text-xs text-green-400 block">UP</span>
                  <span className="text-2xl font-black text-green-400">{market.up_price}¢</span>
                </div>
                <span className="text-gray-600 text-xl">/</span>
                <div>
                  <span className="text-xs text-red-400 block">DOWN</span>
                  <span className="text-2xl font-black text-red-400">{market.down_price}¢</span>
                </div>
              </div>
            </div>

            <div className="bg-gray-900 border border-gray-800 p-6 rounded-2xl shadow-lg relative overflow-hidden group">
              <div className="absolute top-0 left-0 w-1 h-full bg-indigo-500"></div>
              <span className="text-sm text-gray-400 font-medium mb-2 flex items-center justify-between">
                <div>Target (Price to Beat) <span className="ml-2 text-[10px] bg-indigo-500/20 text-indigo-300 px-2 py-0.5 rounded-full border border-indigo-500/30">Click to Edit</span></div>
                {manualTargetRef.current !== null && (
                  <button onClick={(e) => { e.stopPropagation(); manualTargetRef.current = null; setIsTargetEditing(false); }} className="text-xs font-bold bg-gray-800 text-indigo-400 hover:text-white px-2 py-1 rounded">Reset Auto</button>
                )}
              </span>
              <div className="text-3xl font-black text-white tracking-tight flex items-center group-hover:bg-gray-800/30 rounded px-1 transition-colors cursor-text" onClick={() => setIsTargetEditing(true)}>
                <span className="mr-1 text-gray-500">$</span>
                {isTargetEditing ? (
                  <input
                    type="number"
                    step="0.01"
                    autoFocus
                    className="bg-transparent border-b-2 border-indigo-500 focus:outline-none w-full text-white appearance-none"
                    placeholder={market.price_to_beat.toString()}
                    onBlur={(e) => {
                      const val = parseFloat(e.target.value);
                      if (!isNaN(val) && val > 0) {
                        manualTargetRef.current = val;
                        if (ws) ws.send(JSON.stringify({ action: "SYNC_LIVE_GAMMA", target_price: val }));
                        setMarket(prev => ({ ...prev, price_to_beat: val }));
                      }
                      setIsTargetEditing(false);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") e.currentTarget.blur();
                    }}
                  />
                ) : (
                  <span>{market.price_to_beat > 0 ? market.price_to_beat.toFixed(2) : "---"}</span>
                )}
              </div>
            </div>

            <div className="bg-gray-900 border border-gray-800 p-6 rounded-2xl shadow-lg relative overflow-hidden">
              <div className="absolute top-0 left-0 w-1 h-full bg-amber-500"></div>
              <span className="text-sm text-gray-400 font-medium mb-2 flex items-center">
                <Clock className="w-4 h-4 mr-2 text-amber-500" /> Time Remaining
              </span>
              <div className="text-4xl font-black text-amber-400 tracking-tight font-mono">
                {formatTime(market.time_left)}
              </div>
            </div>
          </div>

          {(market.warmup_seconds_left ?? 0) > 0 && (
            <div className="bg-amber-950 border border-amber-700 p-3 rounded-xl flex items-center gap-3 text-amber-300 text-sm font-medium">
              <Clock className="w-4 h-4 shrink-0" />
              Warming up — waiting for price data to settle before trading.&nbsp;
              <span className="font-mono font-bold">{market.warmup_seconds_left}s remaining</span>
            </div>
          )}

          <div className="bg-gray-900 border border-gray-800 p-4 rounded-2xl shadow-lg h-[450px] flex flex-col">
            <h2 className="text-lg font-bold mb-4 px-2 tracking-wide text-gray-200">Live Market Chart</h2>
            <div ref={chartContainerRef} className="w-full flex-grow rounded-xl overflow-hidden" />
          </div>

          {/* User History Panel */}
          <div className="bg-gray-900 border border-gray-800 p-6 rounded-2xl shadow-lg">
            <h2 className="text-xl font-bold mb-4 text-white flex items-center">
              Settled Cycles History (Last 10)
            </h2>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-gray-800 text-sm text-gray-500 uppercase tracking-wider">
                    <th className="p-3">Time</th>
                    <th className="p-3">Winner</th>
                    <th className="p-3">Sunk Cost</th>
                    <th className="p-3">Payout</th>
                    <th className="p-3 text-right">Net Profit</th>
                  </tr>
                </thead>
                <tbody>
                  {portfolio.history.length === 0 ? (
                    <tr><td colSpan={5} className="text-center p-6 text-gray-600">No settled cycles yet. wait 15m.</td></tr>
                  ) : portfolio.history.map((h, i) => (
                    <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/20 transition-colors">
                      <td className="p-3 text-gray-300 table-cell align-middle text-sm">{h.time}</td>
                      <td className="p-3 font-semibold table-cell align-middle text-sm">
                        <span className={h.winning_side === 'UP' ? 'text-emerald-400' : 'text-rose-400'}>{h.winning_side}</span>
                      </td>
                      <td className="p-3 text-gray-400 table-cell align-middle text-sm">${h.spent.toFixed(2)}</td>
                      <td className="p-3 text-gray-300 table-cell align-middle text-sm">${h.payout.toFixed(2)}</td>
                      <td className={`p-3 text-right font-bold table-cell align-middle text-sm ${h.profit > 0 ? 'text-emerald-400' : h.profit < 0 ? 'text-rose-400' : 'text-gray-400'}`}>
                        {h.profit > 0 ? '+' : ''}{h.profit.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Strategy Performance Tracker */}
          <div className="bg-gray-900 border border-gray-800 rounded-3xl p-6 shadow-2xl">
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-xl font-bold text-white">Strategy Performance</h2>
              <button
                onClick={() => ws?.send(JSON.stringify({ action: "RESET_STATS" }))}
                className="text-xs text-gray-500 hover:text-gray-300 border border-gray-700 rounded-lg px-3 py-1 transition-colors"
              >Reset Stats</button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-xs text-gray-500 uppercase tracking-wider">
                    <th className="p-2">Strategy</th>
                    <th className="p-2 text-center">Trades</th>
                    <th className="p-2 text-center">Win %</th>
                    <th className="p-2 text-right">Total P&L</th>
                    <th className="p-2 text-right">Best</th>
                    <th className="p-2 text-right">Worst</th>
                  </tr>
                </thead>
                <tbody>
                  {(() => {
                    const labels: Record<string,string> = {
                      strategy_a: "Smart Balancer", strategy_b: "Momentum Sniper",
                      strategy_c: "Fixed Target", strategy_c_trailing: "C+Trailing",
                      strategy_d: "Strategy D", strategy_e: "Strategy E",
                      strategy_f: "Strategy F", strategy_7: "Strategy 7",
                      strategy_cpt: "CPT", strategy_claude: "Claude AI"
                    };
                    const rows = Object.entries(strategyStats).filter(([,s]) => s.trades > 0);
                    if (rows.length === 0) return (
                      <tr><td colSpan={6} className="text-center p-6 text-gray-600">No trades recorded yet.</td></tr>
                    );
                    return rows.map(([key, s]) => {
                      const winPct = s.trades > 0 ? Math.round((s.wins / s.trades) * 100) : 0;
                      return (
                        <tr key={key} className="border-b border-gray-800/50 hover:bg-gray-800/20 transition-colors">
                          <td className="p-2 font-semibold text-white">{labels[key] ?? key}</td>
                          <td className="p-2 text-center text-gray-400">{s.trades}</td>
                          <td className="p-2 text-center">
                            <span className={winPct >= 60 ? 'text-emerald-400 font-bold' : winPct >= 40 ? 'text-yellow-400' : 'text-rose-400'}>{winPct}%</span>
                          </td>
                          <td className={`p-2 text-right font-bold ${s.total_profit >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                            {s.total_profit >= 0 ? '+' : ''}${s.total_profit.toFixed(2)}
                          </td>
                          <td className="p-2 text-right text-emerald-400">+${s.best.toFixed(2)}</td>
                          <td className={`p-2 text-right ${s.worst < 0 ? 'text-rose-400' : 'text-gray-400'}`}>${s.worst.toFixed(2)}</td>
                        </tr>
                      );
                    });
                  })()}
                </tbody>
              </table>
            </div>
          </div>

        </div>

        {/* Right Column - Trading & Automation */}
        <div className="space-y-6">

          {/* Order Book Panel */}
          <div className="bg-gray-900 border border-gray-800 rounded-3xl p-6 shadow-2xl relative overflow-hidden">
            <div className="absolute -top-20 -right-20 w-40 h-40 bg-blue-500/10 rounded-full blur-3xl"></div>

            <h2 className="text-xl font-bold mb-6 text-white flex items-center">
              Market Prediction
            </h2>

            <div className="space-y-4 relative z-10 transition-all">
              <button
                onClick={buyUp}
                className="w-full group relative overflow-hidden bg-emerald-500/10 hover:bg-emerald-500/20 active:bg-emerald-500/30 border border-emerald-500/20 hover:border-emerald-500/40 rounded-2xl p-5 transition-all duration-300"
              >
                <div className="flex justify-between items-center group-hover:scale-[1.02] transition-transform">
                  <div className="flex items-center">
                    <div className="bg-emerald-500/20 p-2 rounded-xl mr-4">
                      <TrendingUp className="w-6 h-6 text-emerald-400" />
                    </div>
                    <span className="text-2xl font-black text-emerald-400 tracking-wide">Buy Up</span>
                  </div>
                  <span className="text-3xl font-black text-white">{market.up_price}¢</span>
                </div>
              </button>

              <button
                onClick={buyDown}
                className="w-full group relative overflow-hidden bg-rose-500/10 hover:bg-rose-500/20 active:bg-rose-500/30 border border-rose-500/20 hover:border-rose-500/40 rounded-2xl p-5 transition-all duration-300"
              >
                <div className="flex justify-between items-center group-hover:scale-[1.02] transition-transform">
                  <div className="flex items-center">
                    <div className="bg-rose-500/20 p-2 rounded-xl mr-4">
                      <TrendingDown className="w-6 h-6 text-rose-400" />
                    </div>
                    <span className="text-2xl font-black text-rose-400 tracking-wide">Buy Down</span>
                  </div>
                  <span className="text-3xl font-black text-white">{market.down_price}¢</span>
                </div>
              </button>
            </div>

            <div className="mt-6 pt-6 border-t border-gray-800">
              <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-widest mb-4">Your Active Position</h3>

              <div className="grid grid-cols-2 gap-3 mb-4">
                <div className="bg-emerald-950/20 border border-emerald-900/30 p-3 rounded-xl flex flex-col justify-between">
                  <div className="flex justify-between items-start mb-2">
                    <div className="text-[10px] text-emerald-500/70 font-bold uppercase">Up Shares</div>
                    <div className="text-sm font-black text-emerald-400">{portfolio.positions.up.toFixed(2)}</div>
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-xs">
                      <span className="text-gray-500">Value</span>
                      <span className="text-emerald-400/80">${portfolio.live_value_up?.toFixed(2) || '0.00'}</span>
                    </div>
                    <div className="flex justify-between text-xs font-bold border-t border-emerald-900/30 pt-1 mt-1">
                      <span className="text-gray-500 hover:text-gray-400 group-hover:text-emerald-400 transition-colors cursor-help" title={`Spent: $${portfolio.spent?.up?.toFixed(2)}`}>PnL</span>
                      <span className={(portfolio.live_profit_up || 0) > 0 ? 'text-emerald-500' : (portfolio.live_profit_up || 0) < 0 ? 'text-rose-500' : 'text-gray-500'}>
                        {(portfolio.live_profit_up || 0) > 0 ? '+' : ''}{(portfolio.live_profit_up || 0).toFixed(2)}$
                      </span>
                    </div>
                  </div>
                </div>

                <div className="bg-rose-950/20 border border-rose-900/30 p-3 rounded-xl flex flex-col justify-between">
                  <div className="flex justify-between items-start mb-2">
                    <div className="text-[10px] text-rose-500/70 font-bold uppercase">Down Shares</div>
                    <div className="text-sm font-black text-rose-400">{portfolio.positions.down.toFixed(2)}</div>
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-xs">
                      <span className="text-gray-500">Value</span>
                      <span className="text-rose-400/80">${portfolio.live_value_down?.toFixed(2) || '0.00'}</span>
                    </div>
                    <div className="flex justify-between text-xs font-bold border-t border-rose-900/30 pt-1 mt-1">
                      <span className="text-gray-500 hover:text-gray-400 transition-colors cursor-help" title={`Spent: $${portfolio.spent?.down?.toFixed(2)}`}>PnL</span>
                      <span className={(portfolio.live_profit_down || 0) > 0 ? 'text-emerald-500' : (portfolio.live_profit_down || 0) < 0 ? 'text-rose-500' : 'text-gray-500'}>
                        {(portfolio.live_profit_down || 0) > 0 ? '+' : ''}{(portfolio.live_profit_down || 0).toFixed(2)}$
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <div className="bg-gray-950 border border-gray-800 p-4 rounded-xl space-y-3">
                <div className="flex justify-between text-sm">
                  <span className="text-gray-500">Total Sunk Cost</span>
                  <span className="text-gray-300 font-medium">${portfolio.total_spent.toFixed(2)}</span>
                </div>
                <div className="flex justify-between text-sm">
                  <span className="text-gray-500">Live Market Value</span>
                  <span className="text-blue-400 font-medium">${portfolio.live_value.toFixed(2)}</span>
                </div>
                <div className="pt-3 border-t border-gray-800 flex justify-between items-center">
                  <span className="text-gray-400 font-medium uppercase text-xs tracking-wider">Live PNL</span>
                  <span className={`text-xl font-black ${portfolio.live_profit > 0 ? 'text-emerald-500' : portfolio.live_profit < 0 ? 'text-rose-500' : 'text-gray-500'}`}>
                    {portfolio.live_profit > 0 ? '+' : ''}{portfolio.live_profit.toFixed(2)}$
                  </span>
                </div>
              </div>

              {portfolio.total_spent > 0 && (
                <button
                  onClick={cashOut}
                  className="w-full mt-4 bg-indigo-500/20 hover:bg-indigo-500/30 border border-indigo-500/30 font-bold text-indigo-400 py-3 rounded-xl transition-colors tracking-wide flex justify-center items-center"
                >
                  Close Position (Cash Out)
                </button>
              )}

            </div>
          </div>

          {/* Bot Automation Panel */}
          <div className="bg-gray-900 border border-gray-800 rounded-3xl p-6 shadow-xl space-y-6">
            <h2 className="text-xl font-bold text-white flex items-center">
              <Settings2 className="w-5 h-5 mr-3 text-indigo-400" />
              Bot Strategies
            </h2>

            <div
              onClick={() => toggleStrategy('A')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_a
                ? 'bg-indigo-900/40 border-indigo-500'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_a ? 'text-indigo-300' : 'text-gray-300'}`}>Strategy A: Smart Balancer</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_a ? 'bg-indigo-500 border-indigo-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_a && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">Buys both sides across the 1H timeframe at optimal limits to guarantee an arbitrage profit (e.g. ~$1+ per 30 shares).</p>

              {strategies.strategy_a && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-indigo-400">
                  Status: Identifying Spreads...
                </div>
              )}
            </div>

            <div
              onClick={() => toggleStrategy('B')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_b
                ? 'bg-fuchsia-900/40 border-fuchsia-500 shadow-[0_0_15px_rgba(217,70,239,0.2)]'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_b ? 'text-fuchsia-300' : 'text-gray-300'}`}>Strategy B: Momentum</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_b ? 'bg-fuchsia-500 border-fuchsia-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_b && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed">Monitors 1m & 5m trends. Executes sniper buy on &gt;0.15% shifts.</p>
              {strategies.strategy_b && (
                <div className={`mt-3 text-xs font-semibold px-3 py-1 bg-gray-900 inline-block rounded-lg ${strategies.b_done ? 'text-green-400' : 'text-fuchsia-400'}`}>
                  Status: {strategies.b_done ? 'Trade Executed' : 'Scanning Signals...'}
                </div>
              )}
            </div>

            <div
              onClick={() => toggleStrategy('C')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_c
                ? 'bg-emerald-900/40 border-emerald-500'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_c ? 'text-emerald-300' : 'text-gray-300'}`}>Strategy C: Market Maker Arb</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_c ? 'bg-emerald-500 border-emerald-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_c && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">Posts deep limit bids (≤45¢) strictly on both sides to trap risk-free $1.00 payouts.</p>
              {strategies.strategy_c && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-emerald-400">
                  Status: Posting Limits...
                </div>
              )}
            </div>
            <div
              onClick={() => toggleStrategy('C_TRAILING')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_c_trailing
                ? 'bg-teal-900/40 border-teal-500 shadow-[0_0_15px_rgba(20,184,166,0.2)]'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_c_trailing ? 'text-teal-300' : 'text-gray-300'}`}>Strategy C+: Market Maker & Trailing Stop</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_c_trailing ? 'bg-teal-500 border-teal-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_c_trailing && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">Same as Strategy C, but lets profits ride above $1.00 and only exits on a 2¢ drop from the absolute peak profit.</p>
              {strategies.strategy_c_trailing && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-teal-400">
                  Status: Tracking Peak PnL...
                </div>
              )}
            </div>

            <div
              onClick={() => toggleStrategy('CPT')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_cpt
                ? 'bg-sky-900/40 border-sky-500 shadow-[0_0_15px_rgba(56,189,248,0.2)]'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_cpt ? 'text-sky-300' : 'text-gray-300'}`}>Strategy C+ Trail: Smart Hedge</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_cpt ? 'bg-sky-500 border-sky-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_cpt && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">Improved C+ with looser entries (≤55¢), both-side hedging, $3 stop-loss, and simplified micro-trend confirmation.</p>
              {strategies.strategy_cpt && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-sky-400">
                  Status: Smart Hedge Active...
                </div>
              )}
            </div>


            <div
              onClick={() => toggleStrategy('D')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_d
                ? 'bg-amber-900/40 border-amber-500'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_d ? 'text-amber-300' : 'text-gray-300'}`}>Strategy 4: Scalper</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_d ? 'bg-amber-500 border-amber-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_d && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">High-frequency bid scanning for 5% to 15% ROI spread-capture. Flattened at 2m.</p>
              {strategies.strategy_d && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-amber-400">
                  Status: Monitoring Orderbook...
                </div>
              )}
            </div>

            <div
              onClick={() => toggleStrategy('E')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_e
                ? 'bg-rose-900/40 border-rose-500'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_e ? 'text-rose-300' : 'text-gray-300'}`}>Strategy 5: Scalper V2</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_e ? 'bg-rose-500 border-rose-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_e && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">Parallel high-frequency market maker instance mirroring strategy 4 conditions.</p>
              {strategies.strategy_e && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-rose-400">
                  Status: Market Making...
                </div>
              )}
            </div>

            <div
              onClick={() => toggleStrategy('F')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_f
                ? 'bg-teal-900/40 border-teal-500 shadow-[0_0_15px_rgba(20,184,166,0.2)]'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_f ? 'text-teal-300' : 'text-gray-300'}`}>Strategy 6: Trend Scaler</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_f ? 'bg-teal-500 border-teal-500 text-white' : 'border-gray-600'}`}>
                  {strategies.strategy_f && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">Institutional momentum buy above 70¢. Purchases deep OOTM hedges (≤12¢) to protect against vertical fakeouts.</p>
              {strategies.strategy_f && (
                <div className="flex items-center space-x-2 text-xs font-semibold bg-gray-900 inline-block px-3 py-1 rounded-lg text-teal-400">
                  Status: Scanning Trends...
                </div>
              )}
            </div>

            <div
              onClick={() => toggleStrategy('7')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_7
                ? 'bg-blue-900/40 border-blue-500 shadow-[0_0_15px_rgba(59,130,246,0.2)]'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_7
                  ? 'text-blue-300'
                  : 'text-gray-300'
                  }`}>Strategy 7: AMM Pricing Engine</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_7
                  ? 'bg-blue-500 border-blue-500 text-white'
                  : 'border-blue-500'
                  }`}>
                  {strategies.strategy_7 ? (
                    <Check className="w-3 h-3 text-white" />
                  ) : (
                    <Activity className="w-3 h-3 text-blue-500" />
                  )}
                </div>
              </div>
              <p className="text-xs text-gray-400 leading-relaxed mb-3">Mathematical market-maker utilizing Normal Distribution CDF to dynamically re-price options over time.</p>

              <div className="flex items-center space-x-1 bg-gray-900 p-1 rounded-lg border border-gray-700" onClick={(e) => e.stopPropagation()}>
                <span className="text-[10px] text-gray-500 font-bold uppercase px-1">Volatility Tune:</span>
                {[
                  { label: 'Low', value: 0.005 },
                  { label: 'Normal', value: 0.010 },
                  { label: 'High', value: 0.020 }
                ].map(vol => (
                  <button
                    type="button"
                    key={vol.label}
                    onClick={(e) => {
                      e.stopPropagation();
                      changeVolatility(vol.value);
                    }}
                    className={`px-2 py-1 flex-1 rounded text-xs font-bold transition-all duration-300 ${market.volatility === vol.value
                      ? 'bg-blue-600 text-white shadow-md shadow-blue-500/20'
                      : 'text-gray-500 hover:text-gray-300 hover:bg-gray-800'
                      }`}
                  >
                    {vol.label}
                  </button>
                ))}
              </div>
            </div>

            {/* Claude Strategy: AI Confluence */}
            <div
              onClick={() => toggleStrategy('CLAUDE')}
              className={`p-4 rounded-xl border cursor-pointer transition-colors ${strategies.strategy_claude
                ? 'bg-violet-900/40 border-violet-500 shadow-[0_0_15px_rgba(139,92,246,0.3)]'
                : 'bg-gray-800 border-gray-700 hover:border-gray-500'
                }`}
            >
              <div className="flex justify-between items-start mb-2">
                <h3 className={`font-bold ${strategies.strategy_claude
                  ? 'text-violet-300'
                  : 'text-gray-300'
                  }`}>Claude Strategy: AI Confluence</h3>
                <div className={`w-5 h-5 rounded-full border flex items-center justify-center ${strategies.strategy_claude
                  ? 'bg-violet-500 border-violet-500 text-white'
                  : 'border-gray-600'
                  }`}>
                  {strategies.strategy_claude && <Check className="w-3 h-3" />}
                </div>
              </div>
              <p className="text-xs text-gray-500 leading-relaxed mb-3">
                EMA crossover + RSI + multi-timeframe trend confluence. 3-zone profit system, graduated stop-loss, and 10-min timeout rule.
              </p>

              {strategies.strategy_claude && (
                <div className="space-y-2">
                  <div className="flex items-center space-x-3 text-xs font-semibold">
                    <div className={`px-3 py-1 rounded-lg ${
                      strategies.claude_phase === 1 ? 'bg-gray-900 text-yellow-400' :
                      strategies.claude_phase === 2 ? 'bg-gray-900 text-green-400' :
                      strategies.claude_phase === 3 ? 'bg-gray-900 text-orange-400' :
                      'bg-gray-900 text-red-400'
                    }`}>
                      Phase {strategies.claude_phase}/4 {strategies.claude_phase === 1 ? '(Observing)' : strategies.claude_phase === 2 ? '(Trading)' : strategies.claude_phase === 3 ? '(Cautious)' : '(Exit Only)'}
                    </div>
                    <div className={`px-3 py-1 rounded-lg bg-gray-900 ${
                      strategies.claude_confidence >= 75 ? 'text-green-400' :
                      strategies.claude_confidence >= 50 ? 'text-yellow-400' :
                      'text-gray-500'
                    }`}>
                      Confidence: {strategies.claude_confidence}%
                    </div>
                  </div>
                  {strategies.claude_exit_reason && (
                    <div className="text-xs px-3 py-1 rounded-lg bg-gray-900 text-violet-400 font-mono">
                      Last exit: {strategies.claude_exit_reason}
                    </div>
                  )}
                </div>
              )}
            </div>

          </div>

          {/* Cycle Profit Target Config */}
          <div className="bg-gray-900 border border-gray-800 rounded-3xl p-6 shadow-xl mb-6">
            <h2 className="text-xl font-bold mb-4 text-white flex items-center">
              <span className="bg-green-500/20 text-green-400 p-2 rounded-lg mr-3">
                <TrendingUp className="w-5 h-5" />
              </span>
              Target Cycle Profit Limit
            </h2>
            <div className="flex space-x-2">
              {[4, 8, 16, -1].map((val) => (
                <button
                  key={val}
                  type="button"
                  onClick={() => setProfitTarget(val)}
                  className={`flex-1 py-3 rounded-xl font-bold transition-all ${strategies.profit_target === val
                    ? 'bg-green-600 text-white shadow-lg shadow-green-500/30 border border-green-500'
                    : 'bg-gray-800 text-gray-400 border border-gray-700 hover:border-gray-500 hover:text-white'
                    }`}
                >
                  {val === -1 ? 'Unlimited (∞)' : `$${val}`}
                </button>
              ))}
              <div className="relative flex-1">
                <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 font-bold">$</span>
                <input
                  type="number"
                  placeholder="Custom"
                  className={`w-full h-full bg-gray-800 border rounded-xl pl-8 pr-3 text-white font-bold text-center outline-none transition-all ${![4, 8, 16, -1].includes(strategies.profit_target) && strategies.profit_target > 0
                    ? 'bg-gray-800 border-green-500 ring-2 ring-green-500/20 text-white'
                    : 'bg-gray-800 border-gray-700 hover:border-gray-500 focus:border-green-500 text-white'
                    }`}
                  value={![4, 8, 16, -1].includes(strategies.profit_target) && strategies.profit_target > 0 ? strategies.profit_target : ''}
                  onChange={(e) => {
                    const val = parseFloat(e.target.value);
                    if (!isNaN(val) && val > 0) setProfitTarget(val);
                  }}
                />
              </div>
            </div>
            <p className="text-sm text-gray-500 mt-3">
              Sets the combined profit goal for automated strategies per cycle. Once reached, bots will sleep until the next cycle. Select <b className="text-gray-400">Unlimited (∞)</b> to force bots to run continuously.
            </p>
          </div>

        </div>
      </div>
    </div>
  );
}

export default App;
