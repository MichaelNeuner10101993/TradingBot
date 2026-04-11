#!/usr/bin/env python3
import argparse,logging,os,signal,sqlite3,sys,time
from datetime import datetime,timezone
import ccxt
PROJECT_ROOT=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,PROJECT_ROOT)
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT,".env"))
from bot.config import ExchangeConfig,OpsConfig
from bot.data_feed import build_exchange
from bot.ops import setup_logging
from bot.regime import classify_regime
MAKER_FEE,MIN_STEP_PCT,POLL_INTERVAL=0.0016,0.004,30
REGIME_CHECK_INTERVAL=300
EXIT_REGIMES={"BEAR","EXTREME"}
log=logging.getLogger("gridbot")
class GridDB:
    def __init__(self,db_path):
        self.conn=sqlite3.connect(db_path,check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS grid_orders(order_id TEXT PRIMARY KEY,symbol TEXT,side TEXT,price REAL,amount REAL,status TEXT DEFAULT 'open',filled_at TEXT,created_at TEXT,pnl_eur REAL DEFAULT 0.0)")
        self.conn.commit()
    def insert_order(self,oid,sym,side,price,amount):
        self.conn.execute("INSERT OR REPLACE INTO grid_orders(order_id,symbol,side,price,amount,created_at)VALUES(?,?,?,?,?,?)",(oid,sym,side,price,amount,datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
    def mark_filled(self,oid,pnl=0.0):
        self.conn.execute("UPDATE grid_orders SET status='filled',filled_at=?,pnl_eur=? WHERE order_id=?",(datetime.now(timezone.utc).isoformat(),pnl,oid))
        self.conn.commit()
    def mark_cancelled(self,oid):
        self.conn.execute("UPDATE grid_orders SET status='cancelled' WHERE order_id=?",(oid,))
        self.conn.commit()
    def get_open_orders(self):
        return [{"order_id":r[0],"side":r[1],"price":r[2],"amount":r[3]} for r in self.conn.execute("SELECT order_id,side,price,amount FROM grid_orders WHERE status='open'").fetchall()]
    def total_pnl(self):
        r=self.conn.execute("SELECT SUM(pnl_eur) FROM grid_orders WHERE status='filled'").fetchone();return r[0] or 0.0
    def total_trades(self):
        r=self.conn.execute("SELECT COUNT(*) FROM grid_orders WHERE status='filled'").fetchone();return r[0] or 0

def current_price(ex,symbol):
    return float(ex.fetch_ticker(symbol)["last"])

def place_limit_order(ex,symbol,side,amount,price,dry_run=False):
    try:
        pp=float(ex.price_to_precision(symbol,price))
        ap=float(ex.amount_to_precision(symbol,amount))
        if dry_run:
            fid=f"dry-{side[0]}-{int(pp*100):010d}"
            log.info(f"[DRY] {side.upper()} {ap:.6f} @ {pp:.4f} => {fid}")
            return fid
        order=ex.create_limit_order(symbol,side,ap,pp)
        log.info(f"Limit-{side.upper()} {ap:.6f} @ {pp:.4f} => {order[chr(39)+'id'+chr(39)]}")
        return str(order["id"])
    except Exception as e:
        log.error(f"Order-Fehler: {e}")
        return None

def cancel_order(ex,symbol,oid,dry_run=False):
    if dry_run or oid.startswith("dry-"):
        log.info(f"[DRY] Cancel {oid}")
        return True
    try:
        ex.cancel_order(oid,symbol);return True
    except ccxt.OrderNotFound:
        return True
    except Exception as e:
        log.warning(f"Cancel-Fehler: {e}");return False

def get_open_exchange_orders(ex,symbol,dry_run):
    if dry_run:
        return {}
    try:
        return {str(o["id"]): o for o in ex.fetch_open_orders(symbol)}
    except Exception as e:
        log.warning(f"fetch_open_orders Fehler: {e}");return {}

def check_regime(ex,symbol):
    try:
        candles=ex.fetch_ohlcv(symbol,"1h",limit=100)
        if not candles:return "SIDEWAYS"
        regime,_,_=classify_regime(candles);return regime
    except Exception:
        return "SIDEWAYS"

def init_grid(ex,db,symbol,levels,step_pct,amount_eur,dry_run):
    price=current_price(ex,symbol)
    log.info(f"Grid init | Kurs={price:.4f} | L={levels} | Step={step_pct*100:.2f}%")
    ex.load_markets()
    market=ex.markets.get(symbol,{})
    min_cost=((market.get("limits") or {}).get("cost") or {}).get("min") or 5.0
    order_ids=[]
    for i in range(1,levels+1):
        for side,factor in (("buy",1-step_pct*i),("sell",1+step_pct*i)):
            p=price*factor;a=amount_eur/p
            if amount_eur>=min_cost:
                oid=place_limit_order(ex,symbol,side,a,p,dry_run)
                if oid:
                    db.insert_order(oid,symbol,side,p,a);order_ids.append(oid)
    log.info(f"Grid aktiv: {len(order_ids)} Orders");return order_ids

def run_cycle(ex,db,symbol,step_pct,amount_eur,dry_run):
    local_orders=db.get_open_orders()
    if not local_orders:return
    exchange_open=get_open_exchange_orders(ex,symbol,dry_run)
    for order in local_orders:
        oid,side,price,amount=order["order_id"],order["side"],order["price"],order["amount"]
        if dry_run or oid in exchange_open:continue
        try:
            o=ex.fetch_order(oid,symbol)
            if o.get("status") not in ("closed","filled"):
                db.mark_cancelled(oid);continue
            fp=float(o.get("average") or price)
        except Exception:
            fp=price
        fee_cost=fp*amount*MAKER_FEE*2
        pnl=((fp-fp/(1+step_pct))*amount-fee_cost) if side=="sell" else 0.0
        db.mark_filled(oid,pnl_eur=pnl)
        log.info(f"Fill {side.upper()} @ {fp:.4f} | PnL: {pnl:+.4f}EUR | Gesamt: {db.total_pnl():+.4f}EUR")
        ex.load_markets()
        market=ex.markets.get(symbol,{})
        min_cost=((market.get("limits") or {}).get("cost") or {}).get("min") or 5.0
        if amount_eur>=min_cost:
            cp=fp*(1+step_pct) if side=="buy" else fp*(1-step_pct)
            ns="sell" if side=="buy" else "buy"
            ca=amount_eur/cp
            new_oid=place_limit_order(ex,symbol,ns,ca,cp,dry_run)
            if new_oid:db.insert_order(new_oid,symbol,ns,cp,ca)

def cleanup_grid(ex,db,symbol,dry_run):
    open_orders=db.get_open_orders()
    log.info(f"Cleanup: {len(open_orders)} Orders ...")
    for order in open_orders:
        if cancel_order(ex,symbol,order["order_id"],dry_run):
            db.mark_cancelled(order["order_id"])
    log.info(f"PnL: {db.total_pnl():+.4f}EUR | Trades: {db.total_trades()}")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbol",default="ETH/EUR")
    ap.add_argument("--levels",type=int,default=3)
    ap.add_argument("--step",type=float,default=0.008)
    ap.add_argument("--amount",type=float,default=20.0)
    ap.add_argument("--dry-run",action="store_true")
    ap.add_argument("--no-regime-check",action="store_true")
    ap.add_argument("--log-level",default="INFO")
    args=ap.parse_args()
    setup_logging(OpsConfig(log_level=args.log_level))
    log=logging.getLogger("gridbot")
    if args.step<MIN_STEP_PCT:
        log.error(f"Grid-Schritt {args.step*100:.2f}% zu klein! Min: {MIN_STEP_PCT*100:.2f}%")
        sys.exit(1)
    symbol_safe=args.symbol.replace("/","_")
    db_path=os.path.join(PROJECT_ROOT,"db",f"grid_{symbol_safe}.db")
    db=GridDB(db_path)
    log.info(f"Grid-Bot | {args.symbol} | L={args.levels} | {args.step*100:.2f}% | {args.amount:.0f}EUR | {chr(39)+'DRY'+chr(39) if args.dry_run else chr(39)+'LIVE'+chr(39)}")
    ex=build_exchange(ExchangeConfig())
    ex.load_markets()
    if not args.no_regime_check:
        regime=check_regime(ex,args.symbol)
        if regime in EXIT_REGIMES:
            log.warning(f"Regime={regime} => Grid nicht sinnvoll")
            sys.exit(0)
        log.info(f"Regime: {regime} => OK")
    _running=True
    def _stop(sig,frame):
        nonlocal _running
        log.info("Signal => Cleanup ...")
        _running=False
    signal.signal(signal.SIGTERM,_stop)
    signal.signal(signal.SIGINT,_stop)
    init_grid(ex,db,args.symbol,args.levels,args.step,args.amount,args.dry_run)
    last_rc=time.time()
    while _running:
        time.sleep(POLL_INTERVAL)
        if not _running:break
        try:
            run_cycle(ex,db,args.symbol,args.step,args.amount,args.dry_run)
        except Exception as e:
            log.error(f"Zyklus-Fehler: {e}")
        if not args.no_regime_check and time.time()-last_rc>REGIME_CHECK_INTERVAL:
            try:
                regime=check_regime(ex,args.symbol)
                log.info(f"Regime: {regime}")
                if regime in EXIT_REGIMES:
                    log.warning(f"Regime={regime} => Grid stoppt")
                    break
            except Exception as e:
                log.warning(f"Regime-Check Fehler: {e}")
            last_rc=time.time()
    cleanup_grid(ex,db,args.symbol,args.dry_run)
    log.info(f"Beendet | PnL: {db.total_pnl():+.4f}EUR | Trades: {db.total_trades()}")

if __name__=="__main__":
    main()
