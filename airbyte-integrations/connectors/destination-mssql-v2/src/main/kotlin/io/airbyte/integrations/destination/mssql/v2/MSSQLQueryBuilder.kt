/*
 * Copyright (c) 2024 Airbyte, Inc., all rights reserved.
 */

package io.airbyte.integrations.destination.mssql.v2

import io.airbyte.cdk.load.command.DestinationStream
import io.airbyte.cdk.load.data.AirbyteType
import io.airbyte.cdk.load.data.AirbyteValue
import io.airbyte.cdk.load.data.FieldType
import io.airbyte.cdk.load.data.IntegerType
import io.airbyte.cdk.load.data.ObjectType
import io.airbyte.cdk.load.data.ObjectTypeWithoutSchema
import io.airbyte.cdk.load.data.ObjectValue
import io.airbyte.cdk.load.data.StringType
import io.airbyte.cdk.load.data.TimestampTypeWithoutTimezone
import io.airbyte.cdk.load.message.DestinationRecordAirbyteValue
import io.airbyte.integrations.destination.mssql.v2.config.MSSQLConfiguration
import io.airbyte.integrations.destination.mssql.v2.convert.AirbyteTypeToSqlType
import io.airbyte.integrations.destination.mssql.v2.convert.AirbyteValueToStatement.Companion.setAsNullValue
import io.airbyte.integrations.destination.mssql.v2.convert.AirbyteValueToStatement.Companion.setValue
import io.airbyte.integrations.destination.mssql.v2.convert.ResultSetToAirbyteValue.Companion.getAirbyteNamedValue
import io.airbyte.integrations.destination.mssql.v2.convert.SqlTypeToMssqlType
import io.airbyte.protocol.models.v0.AirbyteRecordMessageMeta
import io.airbyte.protocol.models.v0.AirbyteRecordMessageMetaChange
import io.airbyte.protocol.models.Jsons
import java.lang.ArithmeticException
import java.sql.PreparedStatement
import java.sql.ResultSet
import java.sql.Timestamp
import java.time.Instant
import java.util.UUID

class MSSQLQueryBuilder(
    config: MSSQLConfiguration,
    private val stream: DestinationStream,
) {

    companion object {
        val AIRBYTE_RAW_ID = "_airbyte_raw_id"
        val AIRBYTE_EXTRACTED_AT = "_airbyte_extracted_at"
        val AIRBYTE_META = "_airbyte_meta"
        val AIRBYTE_GENERATION_ID = "_airbyte_generation_id"

        val airbyteFinalTableFields =
            listOf(
                NamedField(AIRBYTE_RAW_ID, FieldType(StringType, false)),
                NamedField(AIRBYTE_EXTRACTED_AT, FieldType(IntegerType, false)),
                NamedField(AIRBYTE_META, FieldType(ObjectTypeWithoutSchema, false)),
                NamedField(AIRBYTE_GENERATION_ID, FieldType(IntegerType, false)),
            )

        val airbyteFields = airbyteFinalTableFields.map { it.name }.toSet()

        private fun AirbyteRecordMessageMeta.trackChange(
            fieldName: String,
            change: AirbyteRecordMessageMetaChange.Change,
            reason: AirbyteRecordMessageMetaChange.Reason,
        ) {
            this.changes.add(
                AirbyteRecordMessageMetaChange()
                    .withField(fieldName)
                    .withChange(change)
                    .withReason(reason)
            )
        }
    }

    data class NamedField(val name: String, val type: FieldType)
    data class NamedValue(val name: String, val value: AirbyteValue)

    private val internalSchema: String = config.rawDataSchema
    private val outputSchema: String = stream.descriptor.namespace ?: config.schema
    private val tableName: String = stream.descriptor.name
    private val fqTableName = "$outputSchema.$tableName"

    val finalTableSchema: List<NamedField> =
        airbyteFinalTableFields + extractFinalTableSchema(stream.schema)

    fun createFinalTableIfNotExists(): String =
        createTableIfNotExists(fqTableName, finalTableSchema)

    fun createFinalSchemaIfNotExists(): String = createSchemaIfNotExists(outputSchema)

    fun getFinalTableInsertColumnHeader(): String =
        getFinalTableInsertColumnHeader(fqTableName, finalTableSchema)

    fun populateStatement(
        statement: PreparedStatement,
        record: DestinationRecordAirbyteValue,
        schema: List<NamedField>
    ) {
        val recordObject = record.data as ObjectValue
        var airbyteMetaStatementIndex: Int? = null
        val airbyteMeta =
            AirbyteRecordMessageMeta().apply {
                changes = mutableListOf()
                setAdditionalProperty("syncId", stream.syncId)
            }

        schema.forEachIndexed { index, field ->
            val statementIndex = index + 1
            if (field.name in airbyteFields) {
                when (field.name) {
                    AIRBYTE_RAW_ID ->
                        statement.setString(statementIndex, UUID.randomUUID().toString())
                    AIRBYTE_EXTRACTED_AT ->
                        statement.setLong(
                            statementIndex,
                            record.emittedAtMs
                        )
                    AIRBYTE_GENERATION_ID -> statement.setLong(statementIndex, stream.generationId)
                    AIRBYTE_META -> airbyteMetaStatementIndex = statementIndex
                }
            } else {
                try {
                    val value = recordObject.values[field.name]
                    statement.setValue(statementIndex, value, field.type.type)
                } catch (e: Exception) {
                    statement.setAsNullValue(statementIndex, field.type.type)
                    when (e) {
                        is ArithmeticException ->
                            airbyteMeta.trackChange(
                                field.name,
                                AirbyteRecordMessageMetaChange.Change.NULLED,
                                AirbyteRecordMessageMetaChange.Reason.DESTINATION_FIELD_SIZE_LIMITATION,
                            )
                        else ->
                            airbyteMeta.trackChange(
                                field.name,
                                AirbyteRecordMessageMetaChange.Change.NULLED,
                                AirbyteRecordMessageMetaChange.Reason.DESTINATION_SERIALIZATION_ERROR,
                            )
                    }
                }
            }
        }
        airbyteMetaStatementIndex?.let { statementIndex ->
            statement.setString(statementIndex, Jsons.serialize(airbyteMeta))
        }
    }

    fun readResult(rs: ResultSet, schema: List<NamedField>): ObjectValue {
        val valueMap =
            schema
                .filter { field -> field.name !in airbyteFields }
                .map { field -> rs.getAirbyteNamedValue(field) }
                .associate { it.name to it.value }
        return ObjectValue.from(valueMap)
    }

    fun selectAllRecords(): String = "SELECT * FROM $fqTableName"

    private fun createSchemaIfNotExists(schema: String): String =
        """
            IF NOT EXISTS (SELECT name FROM sys.schemas WHERE name = '$schema')
            BEGIN
                EXEC ('CREATE SCHEMA $schema');
            END
        """.trimIndent()

    private fun createTableIfNotExists(fqTableName: String, schema: List<NamedField>): String =
        """
            IF OBJECT_ID('$fqTableName') IS NULL
            BEGIN
                CREATE TABLE $fqTableName
                (
                    ${airbyteTypeToSqlSchema(schema, separator = ",\n                    ")}
                );
            END
        """.trimIndent()

    private fun getFinalTableInsertColumnHeader(
        fqTableName: String,
        schema: List<NamedField>
    ): String {
        return StringBuilder()
            .apply {
                append("INSERT INTO $fqTableName(")
                append(schema.map { it.name }.joinToString(", "))
                append(") VALUES (")
                append(schema.map { "?" }.joinToString(", "))
                append(")")
            }
            .toString()
    }

    private fun extractFinalTableSchema(schema: AirbyteType): List<NamedField> =
        when (schema) {
            is ObjectType -> {
                (stream.schema as ObjectType)
                    .properties
                    .map { NamedField(name = it.key, type = it.value) }
                    .toList()
            }
            else -> TODO("most likely fail hard")
        }

    private fun airbyteTypeToSqlSchema(schema: List<NamedField>, separator: String): String {
        val toSqlType = AirbyteTypeToSqlType()
        val toMssqlType = SqlTypeToMssqlType()
        return schema
            .map {
                "${it.name} ${toMssqlType.convert(toSqlType.convert(it.type.type)).sqlString} NULL"
            }
            .joinToString(separator = separator)
    }
}
