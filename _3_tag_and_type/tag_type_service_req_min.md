<!-- This is a minimalist requirement document, aiming at letting reader get a overall grasp of core concepts/workflows, and design and implementation requirement, at a few glances-->

This sub-project provides independent tag and type services. Tag and type are commonly used components in multiple kinds of platforms. Tags can be attached to resource objects in a platform, and a resource object can be assumed to be of certain type. In more flexible design, a resource object can have multiple types. For example, a file resource can have type file, but can also have types like 'image', 'png imge', etc. This sub-project assume an resource object can have multiple tags and types.

## Basic Properties and Operations

Basic attribtues of type and tag include user_id, id, name, create time, modify time, is_history_enabled etc.

Tags and types can have parent-child relationships, respectively. Corresponding apis should be supported. Circular parent-child relationship should be prohibited, and attempts to create such kind of relationship should fail.

Following operations should be supported:

- Basic CRUD operations of a tag/type
- Operations related to parent-child relationship of tags and types, respectively.
- Search of type/tags by name.
- other reaonsable operations

## Edit History of tags/types

Changes to the tag/type system should be able to be logged(configured via is_history_enabled property). When logging, the user that performed the operation should be recoreded. Hnece when implemented the operation apis, a user identity should be provided. The change history of a tag/type should be able to be retrieved efficiently. is_history_enabled can be toggled on/off. If attempting to toggle off, all history will be removed. If attempt toggled on, history will start logging from the timepoint. If history logging is turned on, history operations should always be in same transaction with other edit operations, fail to update history should also make the edit operation itself fail.

## Representation of Resource Object's tags/types

A resource object's tags/types should recorded in DynamoDB tables(one for tags, one for types). Each item is like (obj_id, tag_id, lexorank, ...).

We assume the tags/types a resource object have have order, and lexorank is used to represent this order.

We should also support editing history should also be recorded in additional table(s), and edit history of tags/types of one resource object should be able to be retrieved efficiently. The resource object can be an asset managed by another sub-project, and can also be other resource object provided by other sub-projects(some not them might not have been implemented yet, but as long as the table only records resource object id, and there can be a mechanism for querying object type based on obejct it, current object-tag/type recordings and related apis can work properly).

The edit history itself should also support being deleted. But we only support deleting history earlier than given time point. Deleting edit history withint certain time range, or deleting specific edit logs is not supported for the time being.

## Implementation Preference

DynamoDB should be used for data storage. Lambda function should be used to implement apis.

So far the concept of tag and type look very similar(actually almost same), but they should be implemeted relatively independently. In the future they might become different from each other.