<!-- This is a minimalist requirement document, aiming at letting reader get a overall grasp of core concepts/workflows, and design and implementation requiremen, at a few glances-->


This sub-project is an extension of the asset service, and supports collecting assets into 'timelines', a link/list-like structure that collects assets into them. Each asset lies at a certain time point in a belonging timeline.

Each asset can be collected by(sometimes also stated as belong to) multiple timelines. An asset can be collected by no timeline at all.

Althouth in many cases an asset has a time stamp as its inherent property, for flexibility we allow an asset to have different time points in different timeline. One typical example is that for a receipt image, it can have an upload time, and a transaction time(usually recognized using OCR). A scanner timeline that collects assets scanned from specific scanner device should use the upload time, where as a transactions timeline should use the transaction time.

Basic operations that should be able to be performed efficiently include:

- basic CRUD operations on timelines themselves.
- search for timeline by name. at frontend, there should be a general-purposed timeline selector component.
- query of all assets withint given time range. typical queries include query of all assets at given year/month/date in the timeline.
- given a time point(for example time point of a given asset), query given number neighboring assets in timeline, in one direction or in both directions.
- collect asset into a timeline, remove asset from a timeline, change asset time point, etc.

## Data Structure

DynamoDB should used to store information related to timelines. A rough table structure is:

1. One table should record basic information of each timeline, including id, name, create time and time zone, owner user id, name, etc.
2. One table should record the assets each timeline has. In case one asset is collected by multiple timelines, each 'collect' relationship should belong to one item. Typical entries include: timeline id, asset id, time_stamp.

The supported operations should be provided as well-designed RESTful apis with clear names and parameters, implemented in lambda function(s).

## Typical Use Case

Identification and aggregation of timely related related asset. A person takes a photo of something, and then took some notes of the samething. If the photo image from camera app and the note is later uploaded as assets and collected into a default timeline, it will be easily identifiable that the two assets belong to the same event, and can be further processed(for example grouped together to form an article).

The above example assumes a default timeline. But in actual situation the image and the note might be collected from different sources and collected into different timelines. This indicates the potential need to perform operations on multiple timelines as if they are aggregated as one single timeline.

## Other Specs

Time display and storage format should conform to `time-format.md`.
