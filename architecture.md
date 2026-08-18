# **Deutsche Bahn Realtime Train Data – Architecture** 

## **Objective** 

The objective is to collect real-time data specifically for Deutsche Bahn's long-distance trains, primarily ICE, IC and EC, and use this data for historical storage, data analysis and eventually machine-learning applications such as delay prediction. 

## **Data Source** 

We will use Deutsche Bahn's official GTFS and GTFS-Realtime (GTFS-RT) data streams for DB Fernverkehr. This is different from GTFS.DE. GTFS.DE provides an aggregated feed from multiple German transport operators, whereas this approach consumes the data stream provided by Deutsche Bahn for its long-distance transport services. 

## **Architecture** 

```
DEUTSCHE BAHN
      |
      v
DB Fernverkehr
(ICE / IC / EC)
      |
      v
Official DB GTFS-RT
Realtime Feed
      |
      | Protocol Buffers
      v
Python Collector
      |
      +------------------+
      |                  |
GTFS-RT Decoder    Data Processing
      |                  |
      +--------+---------+
               |
               v
        Structured Data
               |
               v
          PostgreSQL
               |
      +--------+---------+
      |                  |
Historical Analysis   Machine Learning
      |                  |
      v                  v
Delay / Route /     Delay Prediction
Station Analysis
```

## **How the Data Flow Works** 

**1.** Deutsche Bahn provides the official realtime feed for DB Fernverkehr. 

**2.** The feed follows the GTFS-Realtime standard, which defines how realtime public-transport information such as delays, trip updates and cancellations is represented. 

**3.** The data is transmitted using Protocol Buffers, a compact binary format suitable for efficiently transmitting structured realtime data. 

**4.** A Python application periodically retrieves the feed through HTTPS. 

**5.** Python decodes the Protocol Buffer data using the GTFS-Realtime bindings. 

**6.** The application extracts information such as Trip ID, service date, Stop ID, arrival/departure information, delay, schedule relationship, and available cancellation or other realtime updates. 

**7.** The processed data can then be stored in PostgreSQL to build a historical dataset. 

**8.** The historical dataset can subsequently be used for train punctuality analysis, station-level delay analysis, delay propagation analysis, route reliability analysis, and delay prediction using machine learning. 

## **Why This Approach** 

The main reason for choosing the DB Fernverkehr GTFS-RT feed is that the project requires Deutsche Bahn-specific data, rather than data from multiple German transport operators. Using the official DB source provides a clear data lineage and avoids web scraping by using a standardized machine-readable realtime data interface. 

```
Deutsche Bahn
      |
DB Fernverkehr
      |
Official GTFS-RT Feed
      |
Python
      |
PostgreSQL
      |
Analytics / Machine Learning
```

## **Technology Stack** 

|**Component**|**Technology**|
|---|---|
|Data provider|Deutsche Bahn|
|Transport scope|DB Fernverkehr|
|Trains|ICE / IC / EC|
|Data standard|GTFS-Realtime|
|Data encoding|Protocol Buffers|
|Ingestion|Python|
|Realtime API access|HTTPS|
|Processing|Python|
|Storage|PostgreSQL|
|Analytics|Python / SQL|
|Future ML|Python / ML frameworks|



## **Data Integration and Analysis** 

The realtime GTFS-RT data will be combined with the corresponding static GTFS timetable data. This allows identifiers such as trip_id and stop_id to be mapped to meaningful train and station information, creating a structured historical dataset for deeper analysis and machine-learning experiments. 

