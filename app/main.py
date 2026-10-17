import os, re, json, calendar, logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
import requests
from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log=logging.getLogger("ledger-agent")
TZ=ZoneInfo("Asia/Kolkata")
DATA_FILE=os.getenv("DATA_FILE", os.path.join(os.path.dirname(os.path.dirname(__file__)),"data.ndjson"))
RATES_URL=os.getenv("RATES_URL","")
AIPIPE_TOKEN=os.getenv("AIPIPE_TOKEN","")
AI_URL=os.getenv("AI_URL","https://aipipe.org/openrouter/v1/chat/completions")
AI_MODEL=os.getenv("AI_MODEL","openrouter/free")
app=FastAPI(title="Acme Appliances Ledger Agent",version="1.0.0")
class QuestionIn(BaseModel): question:str=Field(min_length=1,max_length=4000)
class AnswerOut(BaseModel): answer:Any
ORDERS=[]; RATES={"USD":1.0}

def dt(v):
    try:
        x=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        if x.tzinfo is None: x=x.replace(tzinfo=TZ)
        return x.astimezone(TZ)
    except Exception:return None

def load_orders():
    raw=[]
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,encoding="utf-8") as f:
            for line in f:
                try:
                    x=json.loads(line)
                    if isinstance(x,dict) and x.get("id"):raw.append(x)
                except Exception:pass
    latest={}
    for x in raw:
        old=latest.get(x["id"])
        if old is None or (dt(x.get("updated_at")) or datetime.min.replace(tzinfo=TZ))>(dt(old.get("updated_at")) or datetime.min.replace(tzinfo=TZ)):latest[x["id"]]=x
    return list(latest.values())

def extract_rates(obj):
    if isinstance(obj,dict):
        base=str(obj.get("base",obj.get("base_currency","USD"))).upper()
        for key in ("rates","conversion_rates","data","items"):
            v=obj.get(key)
            if isinstance(v,dict) and v and all(isinstance(n,(int,float)) for n in v.values()):
                vals={str(k).upper():float(n) for k,n in v.items()}
                if base=="USD": return {k:1/n for k,n in vals.items() if n}+{"USD":1.0}
                if "USD" in vals and vals["USD"]: return {k:n/vals["USD"] for k,n in vals.items()}|{base:1.0,"USD":1.0}
            z=extract_rates(v)
            if z:return z
        if "currency" in obj and "rate" in obj:
            try:return {str(obj["currency"]).upper():float(obj["rate"]),"USD":1.0}
            except Exception:pass
    if isinstance(obj,list):
        out={}
        for v in obj:out.update(extract_rates(v))
        return out
    return {}

def load_rates():
    if RATES_URL:
        try:
            r=requests.get(RATES_URL,timeout=6);r.raise_for_status();RATES.update(extract_rates(r.json()))
        except Exception as e:log.warning("FX rates unavailable: %s",e)

ORDERS=load_orders(); load_rates()
log.info("Loaded %d current orders; currencies=%s",len(ORDERS),sorted({x.get('currency','USD') for x in ORDERS}))
def money(n):return float(Decimal(str(n)).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP))
def convert(n,src,dst):
    src=(src or "USD").upper();dst=(dst or src).upper()
    if src==dst:return float(n)
    if src not in RATES or dst not in RATES:return None
    # RATES maps 1 unit of currency to its USD value
    return float(n)*RATES[src]/RATES[dst]

def date_filter(q):
    months={m.lower():i for i,m in enumerate(calendar.month_name) if m};months.update({m.lower():i for i,m in enumerate(calendar.month_abbr) if m})
    m=re.search(r'\b('+'|'.join(sorted(months,key=len,reverse=True))+r')\s+(20\d{2})\b',q,re.I)
    if m:
        mo=months[m.group(1).lower()];y=int(m.group(2));return date(y,mo,1),date(y+1,1,1) if mo==12 else date(y,mo+1,1)
    m=re.search(r'\b(20\d{2})-(\d{2})(?:-(\d{2}))?\b',q)
    if m:
        y,mo=int(m.group(1)),int(m.group(2));d=int(m.group(3) or 1);st=date(y,mo,d)
        return (st,st+timedelta(days=1)) if m.group(3) else (st,date(y+1,1,1) if mo==12 else date(y,mo+1,1))
    m=re.search(r'\b(20\d{2})\b',q)
    if m:return date(int(m.group(1)),1,1),date(int(m.group(1))+1,1,1)
    return None,None

SCHEMA={"type":"object","properties":{
 "metric":{"type":"string","enum":["revenue","refunds","order_count","units_sold","average_order_value","top_product","top_customer","product_breakdown","customer_breakdown","list_products","list_customers","list_regions","count_customers","count_products"]},
 "group_by":{"type":"string","enum":["none","product","customer","region","currency","month"]},
 "region":{"type":["string","null"]},"product":{"type":["string","null"]},"customer":{"type":["string","null"]},"currency":{"type":["string","null"]},"start_date":{"type":["string","null"]},"end_date":{"type":["string","null"]},"limit":{"type":"integer","minimum":1,"maximum":50},"status":{"type":["string","null"]},"sort":{"type":"string","enum":["desc","asc"]}},
 "required":["metric","group_by","region","product","customer","currency","start_date","end_date","limit","status","sort"],"additionalProperties":False}

def ai_plan(q):
    if not AIPIPE_TOKEN:return None
    sys="""Translate the question into the given JSON query plan for Acme order ledger. Use exact product names and customer IDs. Dates are Asia/Kolkata; start_date inclusive and end_date exclusive. For month use first day and first day of next month. Revenue sums amount only for current status=paid. Refunds sum amount for current status=refunded. Other currencies must be converted using FX rates to requested currency. Default revenue currency: preserve currency if unspecified. 'top' defaults to limit 1; 'top N' uses N. List requests use list_products/list_customers/list_regions. Use null for unspecified fields. For 'how many customers/products' use corresponding count metric. Status only when explicitly asked. Default sort desc."""
    try:
        r=requests.post(AI_URL,headers={"Authorization":"Bearer "+AIPIPE_TOKEN,"Content-Type":"application/json"},json={"model":AI_MODEL,"messages":[{"role":"system","content":sys},{"role":"user","content":q}],"response_format":{"type":"json_schema","json_schema":{"name":"ledger_plan","strict":True,"schema":SCHEMA}},"temperature":0},timeout=7)
        r.raise_for_status(); c=r.json()["choices"][0]["message"]["content"]
        return json.loads(c) if isinstance(c,str) else c
    except Exception as e:log.warning("AI planner failed: %s",e);return None

def fallback(q):
    s=q.lower();metric="revenue"
    if "refund" in s:metric="refunds"
    elif any(x in s for x in ["average order","average spend","average value"]):metric="average_order_value"
    elif any(x in s for x in ["how many orders","number of orders","order count","count of orders"]) or re.search(r"how many (?:paid |refunded |void )?orders",s):metric="order_count"
    elif any(x in s for x in ["units sold","quantity sold","how many units"]):metric="units_sold"
    elif any(x in s for x in ["top product","best-selling product","best selling product","most popular product","which product sold most"]):metric="top_product"
    elif any(x in s for x in ["top customer","biggest customer","highest-spending customer","highest spending customer"]):metric="top_customer"
    elif any(x in s for x in ["list products","which products","all products","product names"]):metric="list_products"
    elif any(x in s for x in ["list customers","which customers","all customers","customer ids"]):metric="list_customers"
    elif (any(x in s for x in ["how many customers","number of customers","count customers","distinct customers","unique customers"]) or re.search(r"\bhow many\b.{0,50}\bcustomers\b", s)) and any(x in s for x in ["customer","customers"]):metric="count_customers"
    elif any(x in s for x in ["how many products","number of products","count products"]):metric="count_products"
    elif "product" in s and any(x in s for x in ["breakdown","by product","per product","each product"]):metric="product_breakdown"
    elif "customer" in s and any(x in s for x in ["breakdown","by customer","per customer","each customer"]):metric="customer_breakdown"
    region=next((x.title() for x in ["north","south","east","west","central"] if re.search(r'\b'+x+r'\b',s)),None)
    currency=next((c for c in ["USD","INR","EUR"] if re.search(r'\b'+c+r'\b',q,re.I)),None)
    st,en=date_filter(q);products=sorted({r['product'] for r in ORDERS},key=len,reverse=True)
    product=next((x for x in products if x.lower() in s),None);m=re.search(r'\bC\d{4}\b',q,re.I);customer=m.group().upper() if m else None
    group="product" if metric in ("top_product","product_breakdown") else "customer" if metric in ("top_customer","customer_breakdown") else "none"
    n=re.search(r'\btop\s+(\d+)',s);limit=min(50,int(n.group(1))) if n else 10
    if metric in ("top_product","top_customer"):limit=1
    status=next((x for x in ["paid","refunded","void"] if re.search(r'\b'+x+r'\b',s)),None)
    return {"metric":metric,"group_by":group,"region":region,"product":product,"customer":customer,"currency":currency,"start_date":st.isoformat() if st else None,"end_date":en.isoformat() if en else None,"limit":limit,"status":status,"sort":"asc" if any(x in s for x in ["lowest","least","smallest"]) else "desc"}

def run_plan(p):
    metric=p.get("metric","revenue");target=(p.get("currency") or "").upper() or None;selected=[]
    for orig in ORDERS:
        r=orig;d=dt(r.get("created_at"));ds=d.date().isoformat() if d else None
        if p.get("start_date") and (not ds or ds<p["start_date"]):continue
        if p.get("end_date") and (not ds or ds>=p["end_date"]):continue
        if p.get("region") and str(r.get("region","")).lower()!=str(p["region"]).lower():continue
        if p.get("product") and str(r.get("product","")).lower()!=str(p["product"]).lower():continue
        if p.get("customer") and str(r.get("customer","")).lower()!=str(p["customer"]).lower():continue
        if p.get("status") and str(r.get("status","")).lower()!=str(p["status"]).lower():continue
        if metric=="refunds" and not p.get("status") and r.get("status")!="refunded":continue
        if metric not in ("refunds",) and not p.get("status") and r.get("status")!="paid":continue
        if target:
            a=convert(r.get("amount",0),r.get("currency","USD"),target)
            if a is None:continue
            r=dict(r);r["amount"]=a
        selected.append(r)
    if metric=="list_products":return sorted({r['product'] for r in ORDERS})
    if metric=="list_customers":return sorted({r['customer'] for r in ORDERS})
    if metric=="list_regions":return sorted({r['region'] for r in ORDERS})
    if metric=="count_customers":return len({r['customer'] for r in selected})
    if metric=="count_products":return len({r['product'] for r in selected})
    if metric=="order_count":return len(selected)
    if metric=="units_sold":return sum(int(r.get('qty',0)) for r in selected)
    if metric in ("revenue","refunds"):return money(sum(float(r.get('amount',0)) for r in selected))
    if metric=="average_order_value":return money(sum(float(r.get('amount',0)) for r in selected)/len(selected)) if selected else 0.0
    group=p.get("group_by","none")
    if metric in ("top_product","product_breakdown"):group="product"
    if metric in ("top_customer","customer_breakdown"):group="customer"
    if group=="none":return money(sum(float(r.get('amount',0)) for r in selected))
    sums={}
    for r in selected:
        k=r.get(group)
        if k is not None:sums[k]=sums.get(k,0)+(int(r.get('qty',0)) if metric=="units_sold" else float(r.get('amount',0)))
    result=[{"name":k,"value":int(v) if metric=="units_sold" else money(v)} for k,v in sums.items()]
    result.sort(key=lambda x:(x['value'],x['name']),reverse=p.get('sort','desc')!="asc")
    if metric in ("top_product","top_customer"):return result[0] if result else None
    return result[:max(1,min(50,int(p.get('limit',10) or 10)))]

@app.get("/")
def root():return {"service":"Acme Appliances order ledger agent","ready":bool(ORDERS),"orders_loaded":len(ORDERS),"endpoints":["POST /","POST /answer","GET /healthz"]}
@app.get("/healthz")
def healthz():return {"ok":bool(ORDERS),"orders_loaded":len(ORDERS),"fx_currencies":sorted(RATES)}
@app.post("/",response_model=AnswerOut)
@app.post("/answer",response_model=AnswerOut)
def answer(req:QuestionIn):
    p=ai_plan(req.question) or fallback(req.question)
    for k in ("region","product","customer","currency","status"):
        if isinstance(p.get(k),str) and p[k].strip().lower() in ("","null","none","all"):p[k]=None
    try:result=run_plan(p)
    except Exception:log.exception("Query failed");result=None
    return {"answer":result}
