from flask import Flask, request, send_file
import pandas as pd, io, re, os
from datetime import datetime

app=Flask(__name__)

RAW_REQUIRED=["order_id","delivery_station_code","picklist_created_time","pick_assignment_time",
"staging_time","assignment_to_stage_time_secs","pl_create_to_stage","promise_utr","units_picked"]
AUDIT_REQUIRED=["WarehouseId","Timestamp","LocationId","ASIN","User","Process","ReferenceIdValue"]

def validate(df,cols,name):
    missing=[c for c in cols if c not in df.columns]
    if missing: raise ValueError(f"{name} is missing columns: {', '.join(missing)}")

def estimate_utr(n):
    try:n=float(n)
    except:return 0
    if n<=3:return 120
    if n<=6:return 180
    if n<=10:return 240
    if n<=20:return 360
    if n<=30:return 540
    return 660

def bucket(d):
    if pd.isna(d): return ""
    d=float(d)
    if d<30:return "<30s"
    if d<60:return "30-59s"
    if d<120:return "60-119s"
    if d<180:return "120-179s"
    return "180s+"

def window(h):
    if pd.isna(h):return ""
    h=int(h)
    if h<7:return "12am-7am"
    if h<12:return "7am-12pm"
    if h<15:return "12pm-3pm"
    if h<18:return "3pm-6pm"
    if h<21:return "6pm-9pm"
    return "9pm-12am"

def zone(loc):
    m=re.search(r"P-1-([A-Z])",str(loc))
    return {"A":"Ambient","C":"Chiller","F":"Frozen","V":"Fresh/Veg","D":"Dairy"}.get(m.group(1),"") if m else ""

def aisle(loc):
    s=str(loc)
    return s.split("-")[3] if len(s.split("-"))>3 else (s.split("-")[2] if len(s.split("-"))>2 else "")

def build(raw,audit,store=None):
    validate(raw,RAW_REQUIRED,"Late Slam Raw Data")
    validate(audit,AUDIT_REQUIRED,"Inventory Audit")
    raw=raw.copy(); audit=audit.copy()
    raw["delivery_station_code"]=raw["delivery_station_code"].astype(str)
    if not store: store=raw["delivery_station_code"].value_counts().idxmax()
    raw=raw[raw["delivery_station_code"]==store].copy()
    if raw.empty: raise ValueError(f"No orders found for store {store}")

    for c in ["picklist_created_time","pick_assignment_time","staging_time"]:
        raw[c]=pd.to_datetime(raw[c],errors="coerce")
    raw["units_picked"]=pd.to_numeric(raw["units_picked"],errors="coerce").fillna(0)
    raw["promise_utr"]=pd.to_numeric(raw["promise_utr"],errors="coerce")
    raw["utr_used"]=raw["promise_utr"].fillna(raw["units_picked"].map(estimate_utr))
    raw["assign_stage"]=pd.to_numeric(raw["assignment_to_stage_time_secs"],errors="coerce").fillna(0)
    raw["late"]=raw["assign_stage"]>raw["utr_used"]
    raw["delay"]=(raw["assign_stage"]-raw["utr_used"]).clip(lower=0)
    raw["hour"]=raw["staging_time"].dt.hour
    raw["time_window"]=raw["hour"].map(window)
    raw["upo_bucket"]=raw["units_picked"].map(lambda x: "1-3" if x<=3 else "4-6" if x<=6 else "7-10" if x<=10 else "11-20" if x<=20 else "21-30" if x<=30 else "31+")
    raw["delay_bucket"]=raw["delay"].map(bucket)

    audit["Timestamp"]=pd.to_datetime(audit["Timestamp"],errors="coerce")
    audit["Process"]=audit["Process"].astype(str)
    picks=audit[audit["Process"].str.contains("Outbound Pick",case=False,na=False)].copy()
    picks=picks.sort_values(["ReferenceIdValue","Timestamp"])
    picks["gap"]=picks.groupby("ReferenceIdValue")["Timestamp"].diff().dt.total_seconds()
    picks["zone"]=picks["LocationId"].map(zone)
    picks["aisle"]=picks["LocationId"].map(aisle)
    first=picks.groupby("ReferenceIdValue").first(numeric_only=False)
    last=picks.groupby("ReferenceIdValue").last(numeric_only=False)
    picker=picks.groupby("ReferenceIdValue")["User"].first()
    raw["Picker"]=raw["order_id"].map(picker).fillna("—")
    first_t=raw["order_id"].map(first["Timestamp"]) if "Timestamp" in first else pd.Series(index=raw.index)
    last_t=raw["order_id"].map(last["Timestamp"]) if "Timestamp" in last else pd.Series(index=raw.index)
    raw["assign_to_first"]=((first_t-raw["pick_assignment_time"]).dt.total_seconds()).fillna(0).clip(lower=0)
    raw["pick_duration"]=((last_t-first_t).dt.total_seconds()).fillna(0).clip(lower=0)
    raw["pack_stage"]=((raw["staging_time"]-last_t).dt.total_seconds()).fillna(0).clip(lower=0)
    slow_rows=[]
    for oid,g in picks.groupby("ReferenceIdValue"):
        for _,r in g[g["gap"]>=60].iterrows():
            slow_rows.append([oid,store,raw.loc[raw["order_id"].eq(oid),"Picker"].iloc[0] if any(raw["order_id"].eq(oid)) else "—",
                              "YES" if bool(raw.loc[raw["order_id"].eq(oid),"late"].iloc[0]) if any(raw["order_id"].eq(oid)) else "NO",
                              r["ASIN"],r["LocationId"],r["zone"],r["gap"],"Slow Pick"])
    slow_df=pd.DataFrame(slow_rows,columns=["Order ID","Store","Picker","Is Late Slam","ASIN","Location","Zone","Gap (s)","Flag"])

    # Exact sheet column structure of the supplied reference workbook.
    all_df=pd.DataFrame({
        "Order ID":raw["order_id"],"Store":raw["delivery_station_code"],"Picker":raw["Picker"],
        "Units":raw["units_picked"],"UPO Bucket":raw["upo_bucket"],
        "Drop Time":raw["picklist_created_time"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "Assign Time":raw["pick_assignment_time"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "Stage Time":raw["staging_time"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "Assign→1st Pick (s)":raw["assign_to_first"],"Pick Duration (s)":raw["pick_duration"],
        "Pack/Stage Time (s)":raw["pack_stage"],"Assign→Stage (s)":raw["assign_stage"],
        "PL→Stage (s)":pd.to_numeric(raw["pl_create_to_stage"],errors="coerce").fillna(0),
        "pl_create_to_stage (s)":pd.to_numeric(raw["pl_create_to_stage"],errors="coerce").fillna(0),
        "promise_utr (s)":raw["utr_used"],"Late Slam":raw["late"].map({True:"YES",False:"NO"}),
        "Delay (s)":raw["delay"],"Delay Bucket":raw["delay_bucket"],"Time Window":raw["time_window"],
        "Hour":raw["hour"],"Zone(s)":"", "Slow Picks (ASIN|Loc|Gap)":""})
    late_df=all_df[all_df["Late Slam"].eq("YES")].copy()
    # Match reference sheet's duplicated summary columns on Late Slams Only.
    late_df["Delay"]=late_df["Delay (s)"]
    late_df["Delay Bucket"]=late_df["Delay Bucket"]
    late_df["Time Window"]=late_df["Time Window"]
    late_df["Hour"]=late_df["Hour"]
    late_df["Zones"]=late_df["Zone(s)"]
    late_df["Slow Picks"]=late_df["Slow Picks (ASIN|Loc|Gap)"]

    hours=[]
    for h in range(24):
        x=raw[raw["hour"].eq(h)]
        orders=len(x); l=int(x["late"].sum())
        hours.append([h,window(h),orders,orders,l,round(l/orders*100,2) if orders else 0,0,
                      int(x["units_picked"].sum()),round(x["units_picked"].sum()/50,1) if len(x) else 0,
                      round(x.loc[x["late"],"delay"].mean(),1) if l else 0,""])
    hourly=pd.DataFrame(hours,columns=["Hour","Time Window","FC Orders","Actual Orders","Late Slams","LS Rate %","FC Var %","Units Picked","Pickers Needed (@50 units/hr)","Avg Delay (s)","Top Zone"])

    contrib=picks[picks["ReferenceIdValue"].isin(set(late_df["Order ID"]))].copy()
    if len(contrib):
        ac=contrib.groupby("aisle").size().reset_index(name="Late Slam Count").rename(columns={"aisle":"Aisle"})
        ac["% Contribution"]=ac["Late Slam Count"]/ac["Late Slam Count"].sum()*100
        ac=ac.sort_values("Late Slam Count",ascending=False)
    else: ac=pd.DataFrame(columns=["Aisle","Late Slam Count","% Contribution"])
    ar=picks[picks["ReferenceIdValue"].isin(set(late_df["Order ID"]))][["ReferenceIdValue","LocationId","zone","Timestamp"]].copy()
    ar=ar.merge(raw[["order_id","delivery_station_code","Picker","picklist_created_time","delay"]],
                left_on="ReferenceIdValue",right_on="order_id",how="left")
    aisle_raw=pd.DataFrame({
        "Order ID":ar["ReferenceIdValue"],"Store":ar["delivery_station_code"],
        "Picker":ar["Picker"],"Location":ar["LocationId"],"Aisle":ar["LocationId"].map(aisle),
        "Zone":ar["zone"],"Drop Time":ar["picklist_created_time"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        "Delay (s)":ar["delay"]})

    date=raw["staging_time"].dt.date.dropna().astype(str).mode().iloc[0] if raw["staging_time"].notna().any() else datetime.now().date().isoformat()
    meta={"store":store,"date":date,"total_orders":len(raw),"late_slams":int(raw["late"].sum()),
          "ls_rate":round(raw["late"].mean()*100,2),"slow_picks":len(slow_df)}
    return {"All Orders Raw":all_df,"Late Slams Only":late_df,"Hourly Breakdown":hourly,
            "Slow Picks Detail":slow_df,"Aisle Contribution":ac,"Aisle Raw Data":aisle_raw,"meta":meta}

def write_book(result,out):
    formats={}
    with pd.ExcelWriter(out,engine="xlsxwriter") as w:
        wb=w.book
        header=wb.add_format({"bold":True,"font_color":"white","bg_color":"#4472C4","border":1,"align":"center","valign":"vcenter"})
        red=wb.add_format({"bold":True,"font_color":"white","bg_color":"#C00000","border":1,"align":"center","valign":"vcenter"})
        blue=wb.add_format({"bold":True,"font_color":"white","bg_color":"#2F5496","border":1,"align":"center","valign":"vcenter"})
        orange=wb.add_format({"bold":True,"font_color":"white","bg_color":"#ED7D31","border":1,"align":"center","valign":"vcenter"})
        green=wb.add_format({"bold":True,"font_color":"white","bg_color":"#548235","border":1,"align":"center","valign":"vcenter"})
        body=wb.add_format({"border":1,"align":"center","valign":"vcenter"})
        specs=[("All Orders Raw",header,None),("Late Slams Only",red,"#C00000"),("Hourly Breakdown",blue,"#4472C4"),
               ("Slow Picks Detail",orange,"#ED7D31"),("Aisle Contribution",green,"#548235"),("Aisle Raw Data",green,"#548235")]
        for name,hfmt,tab in specs:
            df=result[name]
            df.to_excel(w,index=False,sheet_name=name)
            ws=w.sheets[name]; ws.freeze_panes(1,0); ws.autofilter(0,0,max(0,len(df.columns)-1),max(1,len(df)))
            ws.set_row(0,22,hfmt)
            if tab: ws.set_tab_color(tab)
            for i,c in enumerate(df.columns):
                width=min(max(len(str(c))+3,12),32)
                ws.set_column(i,i,width,body)
            # date/time-like columns in reference are wider
            for i,c in enumerate(df.columns):
                if "Time" in str(c): ws.set_column(i,i,20,body)
    return out

@app.get("/")
def home(): return send_file("index.html")

@app.post("/generate")
def generate():
    try:
        raw=pd.read_csv(request.files["raw_data_file"])
        audit=pd.read_csv(request.files["inventory_audit_file"])
        r=build(raw,audit,request.form.get("store_code") or None)
        out=io.BytesIO();write_book(r,out);out.seek(0)
        m=r["meta"];fn=f'LS_DeepDive_{m["store"]}_{m["date"]}.xlsx'
        resp=send_file(out,as_attachment=True,download_name=fn,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp.headers["X-Total-Orders"]=str(m["total_orders"]);resp.headers["X-Late-Slams"]=str(m["late_slams"]);resp.headers["X-LS-Rate"]=str(m["ls_rate"])
        return resp
    except Exception as e:return (str(e),400)

if __name__=="__main__":app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
