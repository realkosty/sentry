import {Fragment} from 'react';

import ContextBlock from 'sentry/components/events/contexts/contextBlock';
import {Event} from 'sentry/types/event';

import {geKnownData, getUnknownData} from '../utils';

import {getDeviceKnownDataDetails} from './getDeviceKnownDataDetails';
import {DeviceData, DeviceKnownDataType} from './types';
import {getInferredData} from './utils';

type Props = {
  data: DeviceData;
  event: Event;
};

export const deviceKnownDataValues = [
  DeviceKnownDataType.NAME,
  DeviceKnownDataType.FAMILY,
  DeviceKnownDataType.CPU_DESCRIPTION,
  DeviceKnownDataType.ARCH,
  DeviceKnownDataType.BATTERY_LEVEL,
  DeviceKnownDataType.BATTERY_STATUS,
  DeviceKnownDataType.ORIENTATION,
  DeviceKnownDataType.MEMORY,
  DeviceKnownDataType.MEMORY_SIZE,
  DeviceKnownDataType.FREE_MEMORY,
  DeviceKnownDataType.USABLE_MEMORY,
  DeviceKnownDataType.LOW_MEMORY,
  DeviceKnownDataType.STORAGE_SIZE,
  DeviceKnownDataType.EXTERNAL_STORAGE_SIZE,
  DeviceKnownDataType.EXTERNAL_FREE_STORAGE,
  DeviceKnownDataType.STORAGE,
  DeviceKnownDataType.FREE_STORAGE,
  DeviceKnownDataType.SIMULATOR,
  DeviceKnownDataType.BOOT_TIME,
  DeviceKnownDataType.TIMEZONE,
  DeviceKnownDataType.DEVICE_TYPE,
  DeviceKnownDataType.ARCHS,
  DeviceKnownDataType.BRAND,
  DeviceKnownDataType.CHARGING,
  DeviceKnownDataType.CONNECTION_TYPE,
  DeviceKnownDataType.ID,
  DeviceKnownDataType.LANGUAGE,
  DeviceKnownDataType.MANUFACTURER,
  DeviceKnownDataType.ONLINE,
  DeviceKnownDataType.SCREEN_DENSITY,
  DeviceKnownDataType.SCREEN_DPI,
  DeviceKnownDataType.SCREEN_RESOLUTION,
  DeviceKnownDataType.SCREEN_HEIGHT_PIXELS,
  DeviceKnownDataType.SCREEN_WIDTH_PIXELS,
  DeviceKnownDataType.MODEL,
  DeviceKnownDataType.MODEL_ID,
  DeviceKnownDataType.RENDERED_MODEL,
];

const deviceIgnoredDataValues = [];

export function DeviceEventContext({data, event}: Props) {
  const inferredData = getInferredData(data);
  const meta = event._meta?.contexts?.device ?? {};

  return (
    <Fragment>
      <ContextBlock
        data={geKnownData<DeviceData, DeviceKnownDataType>({
          data: inferredData,
          meta,
          knownDataTypes: deviceKnownDataValues,
          onGetKnownDataDetails: v => getDeviceKnownDataDetails({...v, event}),
        }).map(v => ({
          ...v,
          subjectDataTestId: `device-context-${v.key.toLowerCase()}-value`,
        }))}
      />
      <ContextBlock
        data={getUnknownData({
          allData: inferredData,
          knownKeys: [...deviceKnownDataValues, ...deviceIgnoredDataValues],
          meta,
        })}
      />
    </Fragment>
  );
}
